"""飞书 -> ComfyUI -> Topaz -> 百度网盘 全链路调度器.

用法:
    python main.py                 # 常驻轮询（正常使用）
    python main.py --once          # 只跑一轮，跑完退出（适合定时任务）
    python main.py --record recXXX # 只处理指定记录，调试用
    python main.py --dry-run       # 只自检和列任务，不真跑
    python main.py --no-topaz      # 跳过超分，直接投递 480P（调试链路用）
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from core.comfy import ComfyClient, ComfyError
from core.config import BASE_DIR, Config, ConfigError, load_config, setup_logging
from core.delivery import DeliveryError, NetdiskDelivery
from core.feishu import FeishuBitable, FeishuError
from core.notify import Notifier
from core.state import LockBusy, SingleInstanceLock, StateStore
from core.topaz import TopazError, TopazUpscaler

LOG = logging.getLogger("pipeline")

_STOP = False


def _on_signal(signum: int, _frame: Any) -> None:
    global _STOP
    _STOP = True
    LOG.warning("收到退出信号 %s，处理完当前任务后停止……", signum)


class Pipeline:
    """把四段拼成一条流水线."""

    def __init__(self, cfg: Config, *, skip_topaz: bool = False) -> None:
        self.cfg = cfg
        self.skip_topaz = skip_topaz

        self.feishu = FeishuBitable(cfg)
        self.comfy = ComfyClient(cfg)
        self.topaz = TopazUpscaler(cfg)
        self.delivery = NetdiskDelivery(cfg)
        self.notify = Notifier(cfg)

        self.state = StateStore(cfg.path("runtime.state_file", "state/processed.json"))
        self.max_retries = int(cfg.get("runtime.max_retries", 1))

        keep = cfg.get("runtime.keep_source_dir") or ""
        self.archive_dir = Path(keep) if keep else None

        self.work_dir = BASE_DIR / "state" / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 启动等待 ----------------

    def wait_for_comfy(self, timeout: int = 600, interval: int = 15) -> bool:
        """ComfyUI 没开时循环等待上线，避免流水线一启动就崩。

        返回 True 表示已上线；超时返回 False（之后自检会按硬错误退出）。
        """
        deadline = time.time() + timeout
        while True:
            try:
                self.comfy.ping()
            except Exception:
                if time.time() >= deadline:
                    LOG.error(
                        "等待 ComfyUI 上线超时（%d 秒）。请先启动 ComfyUI"
                        "（默认 http://127.0.0.1:8188）后重试。",
                        timeout,
                    )
                    return False
                left = int(deadline - time.time())
                LOG.warning(
                    "ComfyUI 未连接，等待上线中…（还剩 %d 秒，请启动 ComfyUI）", left
                )
                time.sleep(interval)
                continue
            LOG.info("ComfyUI 已上线，继续")
            return True

    # ---------------- 自检 ----------------

    def self_check(self, *, strict: bool = True) -> bool:
        """启动自检：四段全部探一遍，有问题一次性列全."""
        LOG.info("=" * 56)
        LOG.info("启动自检")
        LOG.info("=" * 56)
        problems: list[str] = []

        # 1. ComfyUI
        try:
            stats = self.comfy.ping()
            dev = (stats.get("devices") or [{}])[0]
            LOG.info(
                "[1/4] ComfyUI    OK  %s | %s",
                (stats.get("system") or {}).get("comfyui_version", "?"),
                dev.get("name", "?"),
            )
        except Exception as exc:
            problems.append(f"ComfyUI 连不上：{exc}")
            LOG.error("[1/4] ComfyUI    FAIL  %s", exc)

        # 2. 工作流
        try:
            wf = self.comfy.load_workflow()
            node = str(self.cfg.get("comfyui.inject.prompt_node", ""))
            if node.startswith("[需填写]") or not node:
                problems.append(
                    "comfyui.inject.prompt_node 还没填，"
                    "先跑 python doctor.py --inspect-workflow 找节点号"
                )
                LOG.error("[2/4] 工作流     FAIL  提示词注入节点未配置")
            elif node not in wf:
                problems.append(f"工作流里没有节点 {node}，检查 inject.prompt_node")
                LOG.error("[2/4] 工作流     FAIL  节点 %s 不存在", node)
            else:
                LOG.info("[2/4] 工作流     OK  %d 个节点，注入点 %s", len(wf), node)
        except Exception as exc:
            problems.append(f"工作流加载失败：{exc}")
            LOG.error("[2/4] 工作流     FAIL  %s", exc)

        # 3. Topaz
        if self.skip_topaz or not self.cfg.get("topaz.enabled", True):
            LOG.info("[3/4] Topaz      跳过（已关闭）")
        else:
            res = self.topaz.check()
            if res.get("ok"):
                LOG.info(
                    "[3/4] Topaz      OK  模型 %s -> %sx%s",
                    self.cfg.get("topaz.model"),
                    self.cfg.get("topaz.target_width"),
                    self.cfg.get("topaz.target_height"),
                )
            else:
                for p in res.get("problems", []):
                    problems.append(f"Topaz：{p}")
                LOG.error("[3/4] Topaz      FAIL  %s", "; ".join(res.get("problems", [])))

        # 4. 网盘
        res = self.delivery.check()
        if res.get("ok"):
            LOG.info("[4/4] 网盘目录   OK  %s", res.get("dir"))
        else:
            for p in res.get("problems", []):
                problems.append(f"网盘：{p}")
            LOG.error("[4/4] 网盘目录   FAIL  %s", "; ".join(res.get("problems", [])))

        # 5. 飞书（放最后，因为它最容易因为没配而失败）
        if not self.feishu.configured:
            problems.append(
                "飞书凭证还没填（app_id / app_secret / app_token / table_id）"
            )
            LOG.error("[+]   飞书表格   FAIL  凭证还是占位符，见 README 第 2 步")
            LOG.info("=" * 56)
            LOG.error("自检发现 %d 个问题：", len(problems))
            for i, p in enumerate(problems, 1):
                LOG.error("  %d. %s", i, p)
            return False

        try:
            info = self.feishu.ping()
            LOG.info(
                "[+]   飞书表格   OK  共 %s 条记录，字段：%s",
                info.get("total"),
                "、".join(info.get("sample_fields", [])[:8]),
            )
        except (ConfigError, FeishuError) as exc:
            problems.append(f"飞书：{exc}")
            LOG.error("[+]   飞书表格   FAIL  %s", exc)
        except Exception as exc:
            problems.append(f"飞书：{exc}")
            LOG.error("[+]   飞书表格   FAIL  %s", exc)

        LOG.info("=" * 56)
        if problems:
            LOG.error("自检发现 %d 个问题：", len(problems))
            for i, p in enumerate(problems, 1):
                LOG.error("  %d. %s", i, p)
            if strict:
                LOG.error("修好上面的问题再启动。只想看看的话加 --dry-run")
            return False

        # 6. 完成通知（可选，仅告警、不阻断启动）
        if self.cfg.get("notify.enabled", False):
            if self.cfg.get("notify.channel") == "feishu_webhook":
                url = self.cfg.get("notify.feishu_webhook_url", "")
                if not url or str(url).startswith("[需填写]"):
                    LOG.warning(
                        "[6]   完成通知   WARN  已开启但未配置 webhook，"
                        "通知不会发送（不影响主流程）"
                    )
                else:
                    LOG.info("[6]   完成通知   OK  webhook 已配置")
            else:
                LOG.warning(
                    "[6]   完成通知   WARN  未知渠道 %s",
                    self.cfg.get("notify.channel"),
                )
        else:
            LOG.info("[6]   完成通知   跳过（未开启）")

        LOG.info("自检全部通过，可以干活了")
        return True

    # ---------------- 单条任务 ----------------

    def process(self, record: dict[str, Any]) -> None:
        rid = record["record_id"]
        prompt = (record.get("prompt") or "").strip()
        negative = (record.get("negative") or "").strip()
        seed_raw = record.get("seed")
        ratio = (record.get("ratio") or "").strip()
        video_len_raw = (record.get("video_len") or "").strip()
        resolution = (record.get("resolution") or "").strip()
        upscale_raw = (record.get("upscale") or "").strip()

        if not prompt:
            LOG.warning("[%s] 提示词为空，跳过", rid)
            self.feishu.mark_failed(rid, "提示词为空")
            self.state.mark_done(rid)
            return

        try:
            seed = int(seed_raw) if seed_raw not in (None, "", 0) else None
        except (TypeError, ValueError):
            seed = None

        # 视频时长（秒）：从表单文字里抽第一个整数，限幅 1~60
        video_len: int | None = None
        if video_len_raw:
            digits = re.findall(r"\d+", video_len_raw)
            if digits:
                try:
                    v = int(digits[0])
                    if 1 <= v <= 60:
                        video_len = v
                except ValueError:
                    pass

        LOG.info("-" * 56)
        LOG.info("[%s] 开始处理", rid)
        LOG.info("[%s] 提示词: %s", rid, prompt[:120] + ("…" if len(prompt) > 120 else ""))
        if negative:
            LOG.info("[%s] 反向:   %s", rid, negative[:80])
        if seed:
            LOG.info("[%s] 种子:   %s", rid, seed)
        if ratio:
            LOG.info("[%s] 比例:   %s", rid, ratio)
        if video_len:
            LOG.info("[%s] 时长:   %s 秒", rid, video_len)
        if resolution:
            LOG.info("[%s] 分辨率: %s", rid, resolution)
        if upscale_raw:
            LOG.info("[%s] 超分:   %s", rid, upscale_raw)

        t0 = time.time()
        self.feishu.mark_running(rid)

        # --- 1. ComfyUI 生成（断点续跑：后续步骤失败重试时复用已生成的 480P，不重复生成）---
        cached = self.state.get_source(rid)
        if cached and Path(cached).exists():
            src = Path(cached)
            LOG.info(
                "[%s] 1/3 复用上次已生成的 480P 原片（断点续跑，不重复生成）: %s",
                rid,
                src.name,
            )
        else:
            LOG.info("[%s] 1/3 ComfyUI 生成中……", rid)
            src = self.comfy.generate(
                prompt, negative, seed, ratio or None, video_len, resolution or None
            )
            self.state.set_source(rid, str(src))
            LOG.info("[%s] 1/3 完成 %s（%.0f 秒）", rid, src.name, time.time() - t0)

        # --- 2. Topaz 超分（全局开关 + 表单逐条选择共同决定）---
        # 表单选「否 / 不 / 关 / no / false / 0」则该条跳过超分，直接投递原片
        do_upscale = (not self.skip_topaz) and bool(self.cfg.get("topaz.enabled", True))
        if upscale_raw:
            neg = ("否" in upscale_raw or "不" in upscale_raw or "关" in upscale_raw
                   or upscale_raw.strip().lower() in ("no", "false", "0", "off"))
            if neg:
                do_upscale = False
        if not do_upscale:
            LOG.info("[%s] 2/3 跳过超分（表单选择不超分或超分已关闭）", rid)
            final_src = src
        else:
            t1 = time.time()
            info = self.topaz.probe(src)
            if info:
                LOG.info(
                    "[%s] 2/3 源片 %sx%s %.1fs，开始超分……",
                    rid,
                    info.get("width"),
                    info.get("height"),
                    float(info.get("duration") or 0),
                )
            else:
                LOG.info("[%s] 2/3 开始超分……", rid)

            out = self.work_dir / f"{rid}_1080p.mp4"
            final_src = self.topaz.upscale(src, out)
            LOG.info("[%s] 2/3 完成（%.0f 秒）", rid, time.time() - t1)

        # --- 3. 投递网盘 ---
        filename = self.delivery.build_filename(prompt, rid)
        final = self.delivery.deliver(final_src, filename)
        netdisk = self.delivery.netdisk_path(final)
        LOG.info("[%s] 3/3 已投递 %s", rid, netdisk)

        # --- 4. 归档 480P 原片 ---
        self._archive(src, rid)

        # --- 5. 清理中间文件 ---
        if final_src != src and final_src.exists() and final_src.parent == self.work_dir:
            try:
                final_src.unlink()
            except OSError:
                pass

        # --- 6. 回写飞书 ---
        total = time.time() - t0
        self.feishu.mark_done(
            rid, filename=final.name, netdisk_path=netdisk, duration=total
        )
        self.state.mark_done(rid)
        self.state.clear_source(rid)  # 整条成功，清除断点，下次彻底重新生成

        # --- 7. 完成通知（飞书群机器人）---
        try:
            self.notify.notify_success(
                prompt=prompt,
                ratio=ratio,
                video_len=video_len,
                netdisk_path=netdisk,
                duration=total,
                filename=final.name,
                resolution=resolution if resolution else "480P",
                upscale="是" if do_upscale else "否",
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[%s] 发送完成通知失败（不影响主流程）: %s", rid, exc)

        LOG.info("[%s] 全部完成，总耗时 %.0f 秒（%.1f 分钟）", rid, total, total / 60)
        LOG.info(
            "[%s] 网盘客户端约需 %s 秒上传，手机上稍等即可看到",
            rid,
            self.cfg.get("baidu.upload_wait_hint", 60),
        )

    def _archive(self, src: Path, rid: str) -> None:
        """把 480P 原片挪到归档目录."""
        if not self.archive_dir:
            return
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = self.archive_dir / f"{stamp}_{rid}_src{src.suffix}"
            shutil.copy2(src, dst)
            LOG.debug("原片已归档: %s", dst)
        except OSError as exc:
            LOG.warning("原片归档失败（不影响主流程）: %s", exc)

    def process_safe(self, record: dict[str, Any]) -> bool:
        """带重试和异常兜底的单条处理."""
        rid = record["record_id"]
        attempts = self.max_retries + 1
        reason = "未知错误"

        for attempt in range(1, attempts + 1):
            try:
                self.process(record)
                return True
            except (ComfyError, TopazError, DeliveryError) as exc:
                reason = str(exc)
                LOG.error("[%s] 第 %d/%d 次失败: %s", rid, attempt, attempts, reason)
            except FeishuError as exc:
                LOG.error("[%s] 飞书接口出错: %s", rid, exc)
                return False
            except Exception as exc:  # noqa: BLE001
                reason = f"{type(exc).__name__}: {exc}"
                LOG.error("[%s] 第 %d/%d 次异常: %s", rid, attempt, attempts, reason)
                LOG.debug(traceback.format_exc())

            if attempt < attempts and not _STOP:
                LOG.info("[%s] 10 秒后重试……", rid)
                time.sleep(10)

        # 全部重试用尽
        try:
            self.feishu.mark_failed(rid, reason)
            self.notify.notify_failure(
                prompt=(record.get("prompt") or ""),
                reason=reason,
                record_id=rid,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.error("[%s] 回写失败状态也失败了: %s", rid, exc)
        self.state.mark_failed(rid)
        self.state.clear_source(rid)  # 彻底失败，清断点，手动重置后会重新生成
        return False

    # ---------------- 主循环 ----------------

    def run_once(self, *, dry_run: bool = False) -> int:
        """跑一轮，返回本轮处理的条数."""
        if not self.feishu.configured:
            LOG.error("飞书还没配好，没法拉任务。跑 python doctor.py 看缺什么")
            return 0
        try:
            records = self.feishu.fetch_pending()
        except FeishuError as exc:
            LOG.error("拉取待处理任务失败: %s", exc)
            return 0

        # 过滤掉状态文件里已完成的（飞书回写失败时的兜底）
        fresh = [r for r in records if not self.state.is_done(r["record_id"])]
        skipped = len(records) - len(fresh)
        if skipped:
            LOG.warning("跳过 %d 条本地已标记完成、但飞书状态没更新的记录", skipped)

        if not fresh:
            return 0

        LOG.info("发现 %d 条待处理任务", len(fresh))

        if dry_run:
            for r in fresh:
                LOG.info(
                    "  [dry-run] %s | %s",
                    r["record_id"],
                    (r.get("prompt") or "")[:60],
                )
            return 0

        done = 0
        for r in fresh:
            if _STOP:
                LOG.warning("收到退出信号，剩余 %d 条留到下次", len(fresh) - done)
                break
            if self.process_safe(r):
                done += 1
        return done

    def run_forever(self) -> None:
        interval = int(self.cfg.get("feishu.poll_interval", 30))
        LOG.info("进入轮询模式，每 %d 秒查一次飞书。Ctrl+C 退出", interval)

        idle_logged = False
        while not _STOP:
            try:
                n = self.run_once()
                if n:
                    LOG.info("本轮处理 %d 条，继续待命", n)
                    idle_logged = False
                elif not idle_logged:
                    LOG.info("暂无待处理任务，静默待命中……")
                    idle_logged = True
            except Exception as exc:  # noqa: BLE001
                LOG.error("轮询出错（不影响继续运行）: %s", exc)
                LOG.debug(traceback.format_exc())

            # 分片 sleep，保证 Ctrl+C 能及时响应
            slept = 0.0
            while slept < interval and not _STOP:
                time.sleep(0.5)
                slept += 0.5

        LOG.info("已退出")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="飞书 -> ComfyUI -> Topaz -> 百度网盘 全链路调度器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default=None, help="配置文件路径，默认 config.yaml")
    p.add_argument("--once", action="store_true", help="只跑一轮就退出")
    p.add_argument("--record", default=None, help="只处理指定 record_id")
    p.add_argument("--dry-run", action="store_true", help="只自检和列任务，不真跑")
    p.add_argument("--no-topaz", action="store_true", help="跳过超分，直接投递原片")
    p.add_argument("--skip-check", action="store_true", help="跳过启动自检")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2

    log_file = setup_logging(cfg)
    LOG.info("日志文件: %s", log_file)

    signal.signal(signal.SIGINT, _on_signal)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
    except (AttributeError, ValueError):
        pass

    try:
        pipe = Pipeline(cfg, skip_topaz=args.no_topaz)
    except ConfigError as exc:
        LOG.error("初始化失败: %s", exc)
        return 2

    # 先等 ComfyUI 上线（没开就一直等，避免一启动就崩）
    if (
        not args.skip_check
        and not args.dry_run
        and cfg.get("runtime.check_on_start", True)
    ):
        pipe.wait_for_comfy(timeout=int(cfg.get("runtime.wait_comfy_timeout", 600)))

    # 自检
    if not args.skip_check and cfg.get("runtime.check_on_start", True):
        ok = pipe.self_check(strict=not args.dry_run)
        if not ok and not args.dry_run:
            return 3

    if args.dry_run:
        LOG.info("dry-run 模式，列一下待处理任务：")
        pipe.run_once(dry_run=True)
        return 0

    lock = SingleInstanceLock(BASE_DIR / "state" / "pipeline.lock")
    try:
        lock.acquire()
    except LockBusy as exc:
        LOG.error("%s", exc)
        return 4

    try:
        if args.record:
            LOG.info("单条调试模式: %s", args.record)
            records = pipe.feishu.fetch_pending(limit=100)
            hit = next((r for r in records if r["record_id"] == args.record), None)
            if not hit:
                LOG.error("待处理列表里没找到 %s（可能状态不是「待处理」）", args.record)
                return 5
            return 0 if pipe.process_safe(hit) else 1

        if args.once:
            n = pipe.run_once()
            LOG.info("单轮结束，处理 %d 条", n)
            return 0

        pipe.run_forever()
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
