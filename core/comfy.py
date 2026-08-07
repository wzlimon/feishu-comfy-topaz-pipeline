"""ComfyUI HTTP API 客户端.

流程：注入提示词 -> POST /prompt -> 轮询 /history -> 定位产出视频。
"""

from __future__ import annotations

import copy
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from .config import Config

LOG = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".gif"}

# 工作流里常见的提示词字段名，用于自动探测
PROMPT_KEYS = (
    "prompt",
    "text",
    "positive",
    "positive_prompt",
    "text_positive",
    "string",
)


class ComfyError(Exception):
    """ComfyUI 执行失败."""


class ComfyClient:
    """ComfyUI 客户端."""

    def __init__(self, cfg: Config) -> None:
        host = cfg.get("comfyui.host", "127.0.0.1")
        port = int(cfg.get("comfyui.port", 8188))
        self.base = f"http://{host}:{port}"

        self.workflow_path = cfg.path("comfyui.workflow", "workflows/t2v.json")
        self.output_dir = Path(str(cfg.get("comfyui.output_dir", "")))
        self.timeout = int(cfg.get("comfyui.timeout", 900))
        self.poll_interval = float(cfg.get("comfyui.poll_interval", 3))

        inj = cfg.get("comfyui.inject", {}) or {}
        self.prompt_node = str(inj.get("prompt_node", "") or "").strip()
        self.prompt_field = str(inj.get("prompt_field", "prompt") or "prompt")
        self.negative_node = str(inj.get("negative_node", "") or "").strip()
        self.negative_field = str(
            inj.get("negative_field", "negative_prompt") or "negative_prompt"
        )
        self.seed_node = str(inj.get("seed_node", "") or "").strip()
        self.seed_field = str(inj.get("seed_field", "seed") or "seed")

        # 比例 / 时长（可选注入）
        self.ratio_node = str(inj.get("ratio_node", "") or "").strip()
        self.ratio_field = str(inj.get("ratio_field", "aspect_ratio") or "aspect_ratio")
        self.ratio_options = inj.get("ratio_options") or {}
        self.ratio_default = str(inj.get("ratio_default", "") or "").strip()
        self.duration_node = str(inj.get("duration_node", "") or "").strip()
        self.duration_field = str(inj.get("duration_field", "value") or "value")
        self.duration_default = inj.get("duration_default", None)

        # 分辨率 / 质量（megapixels）
        self.resolution_node = str(inj.get("resolution_node", "") or "").strip()
        self.resolution_field = str(
            inj.get("resolution_field", "megapixels") or "megapixels"
        )
        self.resolution_options = inj.get("resolution_options") or {}
        try:
            self.resolution_default = float(inj.get("resolution_default", 0.4))
        except (TypeError, ValueError):
            self.resolution_default = 0.4

        self.client_id = str(uuid.uuid4())
        self._session = requests.Session()

    # ---------------- 基础 ----------------

    def ping(self) -> dict[str, Any]:
        """检查服务是否在线，顺带返回显卡信息."""
        resp = self._session.get(f"{self.base}/system_stats", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def load_workflow(self) -> dict[str, Any]:
        """读取 API 格式工作流."""
        if not self.workflow_path.exists():
            raise ComfyError(
                f"找不到工作流文件: {self.workflow_path}\n"
                "  >> 在 ComfyUI 里打开你的文生视频工作流，"
                "用「工作流 -> 导出(API)」或 Save (API Format) 导出，放到 workflows/ 下"
            )
        with self.workflow_path.open("r", encoding="utf-8") as fh:
            wf = json.load(fh)

        if not isinstance(wf, dict) or not wf:
            raise ComfyError("工作流 JSON 格式不对，应该是 {节点ID: {...}} 的字典")

        first = next(iter(wf.values()))
        if not isinstance(first, dict) or "class_type" not in first:
            raise ComfyError(
                "这份 JSON 不是 API 格式（节点里没有 class_type）。\n"
                "  >> 必须用「导出(API)」/ Save (API Format)，"
                "普通的保存/导出格式不能直接提交"
            )
        return wf

    def inspect_workflow(self) -> list[dict[str, Any]]:
        """列出工作流里所有含文本输入的节点，方便定位提示词注入点."""
        wf = self.load_workflow()
        found: list[dict[str, Any]] = []
        for node_id, node in wf.items():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {}) or {}
            text_inputs = {
                k: v
                for k, v in inputs.items()
                if isinstance(v, str) and len(v) > 0
            }
            other_scalars = {
                k: v for k, v in inputs.items() if isinstance(v, (int, float, bool))
            }
            if text_inputs or other_scalars:
                found.append(
                    {
                        "node_id": node_id,
                        "class_type": node.get("class_type", "?"),
                        "title": (node.get("_meta") or {}).get("title", ""),
                        "text_inputs": text_inputs,
                        "scalar_inputs": other_scalars,
                        "likely_prompt": any(
                            k in PROMPT_KEYS for k in text_inputs
                        ),
                    }
                )
        return found

    # ---------------- 注入 ----------------

    @staticmethod
    def _normalize_ratio(raw: str) -> str:
        """把各种写法归一化，提取核心比例 W:H（如 '9:16'）。

        兼容：全角冒号、空格、中文方向词（竖屏/横屏/方形/标清…）。
        """
        s = (raw or "").strip()
        s = s.replace("：", ":")  # 全角 -> 半角
        s = s.replace("＊", "*").replace("x", ":").replace("X", ":")
        for junk in [
            "竖屏", "横屏", "方形", "超宽屏", "宽屏",
            "标清", "高清", "标准", "屏", " ", "　",
            "（", "）", "(", ")",
        ]:
            s = s.replace(junk, "")
        m = re.search(r"(\d+)\s*[:：]\s*(\d+)", s)
        if m:
            return f"{int(m.group(1))}:{int(m.group(2))}"
        return ""

    def _resolve_ratio(self, raw: str) -> str:
        """把表单值解析成 ResolutionSelector 接受的 aspect_ratio 字符串。"""
        if not raw:
            return ""
        raw = raw.strip()
        # 1) 精确匹配友好名（config 里 ratio_options 的键）
        if raw in self.ratio_options:
            return self.ratio_options[raw]
        # 2) 已经是合法值（如 '9:16 (Portrait Widescreen)'）
        if raw in set(self.ratio_options.values()):
            return raw
        # 3) 归一化后按核心 W:H 匹配
        core = self._normalize_ratio(raw)
        if core:
            for val in self.ratio_options.values():
                if val.startswith(core + " ") or val.startswith(core + "("):
                    return val
        return ""

    def _resolve_resolution(self, raw: str) -> float | None:
        """把表单值解析成 megapixels 数值（如 0.4 / 2.0）。

        支持：友好名（config 里 resolution_options 的键，如 '480P'）、
        直接数字（'0.4' / '2' / '1.5'），以及带单位的写法（'1080P' -> 2.0）。
        范围限制在 0.1~4.0 之间（超出视为非法，回退默认）。
        """
        if not raw:
            return None
        raw = str(raw).strip()
        # 1) 友好名精确匹配
        if raw in self.resolution_options:
            try:
                return float(self.resolution_options[raw])
            except (TypeError, ValueError):
                return None
        # 2) 直接数字或带单位的数字（提取第一个浮点数）
        m = re.search(r"(\d+(?:\.\d+)?)", raw)
        if m:
            try:
                f = float(m.group(1))
                if 0.1 <= f <= 4.0:
                    return f
            except ValueError:
                pass
        return None

    def build_prompt(
        self,
        prompt: str,
        negative: str = "",
        seed: int | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
    ) -> dict[str, Any]:
        """把提示词/比例/时长/分辨率/种子注入工作流副本."""
        wf = copy.deepcopy(self.load_workflow())

        if not self.prompt_node:
            raise ComfyError(
                "config.yaml 里 comfyui.inject.prompt_node 没填。\n"
                "  >> 运行 python doctor.py --inspect-workflow 查看候选节点"
            )
        if self.prompt_node not in wf:
            raise ComfyError(
                f"工作流里没有节点 {self.prompt_node}，"
                f"现有节点: {', '.join(sorted(wf.keys())[:20])}"
            )

        node = wf[self.prompt_node]
        node.setdefault("inputs", {})
        if self.prompt_field not in node["inputs"]:
            LOG.warning(
                "节点 %s 原本没有字段 %s，将新增（可能不生效，请核对）",
                self.prompt_node,
                self.prompt_field,
            )
        node["inputs"][self.prompt_field] = prompt
        LOG.info("提示词已注入节点 %s.%s", self.prompt_node, self.prompt_field)

        if negative and self.negative_node and self.negative_node in wf:
            wf[self.negative_node].setdefault("inputs", {})
            wf[self.negative_node]["inputs"][self.negative_field] = negative
            LOG.info("反向提示词已注入节点 %s", self.negative_node)

        # 种子：显式指定就用指定值，否则随机，避免相同提示词出一样的片子
        if self.seed_node and self.seed_node in wf:
            actual = seed if seed else random.randint(1, 2**31 - 1)
            wf[self.seed_node].setdefault("inputs", {})
            wf[self.seed_node]["inputs"][self.seed_field] = actual
            LOG.info("种子 %s 已注入节点 %s", actual, self.seed_node)

        # 比例：把表单选的友好名/简写映射成节点接受的 aspect_ratio 字符串，
        # 写入 ResolutionSelector(115)。该节点会按自身算法产出宽高并连到 MiniMax(105:104)，
        # 沿用其已验证的尺寸（16:9->864x480 / 9:16->480x864），不自行计算宽高以免破坏可用链路。
        if self.ratio_node and self.ratio_node in wf:
            raw = (ratio or "").strip() or self.ratio_default
            actual = self._resolve_ratio(raw)
            if actual:
                wf[self.ratio_node].setdefault("inputs", {})[
                    self.ratio_field
                ] = actual
                LOG.info(
                    "比例已注入节点 %s.%s = %s (表单值=%r)",
                    self.ratio_node,
                    self.ratio_field,
                    actual,
                    raw,
                )
            else:
                # 解析不出：回退默认，避免「没注入却以为成功了」
                if self.ratio_default:
                    wf[self.ratio_node].setdefault("inputs", {})[
                        self.ratio_field
                    ] = self.ratio_default
                    LOG.warning(
                        "比例「%s」无法识别，已用默认 %s", raw, self.ratio_default
                    )

        # 时长（秒）：注入到 PrimitiveFloat 节点的 value 字段
        if self.duration_node and self.duration_node in wf:
            dur = duration if duration else self.duration_default
            if dur:
                try:
                    dur_int = int(dur)
                    wf[self.duration_node].setdefault("inputs", {})[
                        self.duration_field
                    ] = dur_int
                    LOG.info("时长 %s 秒已注入节点 %s", dur_int, self.duration_node)
                except (TypeError, ValueError):
                    LOG.warning("时长值无效，跳过: %r", dur)

        # 分辨率 / 质量（megapixels）：注入到 ResolutionSelector(115)
        #   0.4≈480P，2.0≈1080P。该节点会用它（结合 aspect_ratio）算出具体宽高，
        #   沿用其已验证的算法，不自行算宽高。
        if self.resolution_node and self.resolution_node in wf:
            raw = (resolution or "").strip()
            val = self._resolve_resolution(raw)
            target = val if val is not None else self.resolution_default
            wf[self.resolution_node].setdefault("inputs", {})[
                self.resolution_field
            ] = target
            if val is None and raw:
                LOG.warning(
                    "分辨率「%s」无法识别，已用默认 %s", raw, self.resolution_default
                )
            else:
                LOG.info(
                    "分辨率已注入节点 %s.%s = %s (表单值=%r)",
                    self.resolution_node,
                    self.resolution_field,
                    target,
                    raw,
                )

        return wf

    # ---------------- 执行 ----------------

    def submit(self, workflow: dict[str, Any]) -> str:
        """提交任务，返回 prompt_id."""
        payload = {"prompt": workflow, "client_id": self.client_id}
        resp = self._session.post(f"{self.base}/prompt", json=payload, timeout=60)

        if resp.status_code != 200:
            detail = resp.text[:1500]
            try:
                err = resp.json()
                node_errors = err.get("node_errors") or {}
                if node_errors:
                    detail = json.dumps(node_errors, ensure_ascii=False, indent=2)
                elif err.get("error"):
                    detail = json.dumps(err["error"], ensure_ascii=False)
            except ValueError:
                pass
            raise ComfyError(f"提交失败 HTTP {resp.status_code}:\n{detail}")

        data = resp.json()
        pid = data.get("prompt_id")
        if not pid:
            raise ComfyError(f"提交成功但没拿到 prompt_id: {data}")
        LOG.info("任务已提交 prompt_id=%s", pid)
        return pid

    def wait(self, prompt_id: str) -> dict[str, Any]:
        """轮询直到任务完成，返回 history 条目."""
        deadline = time.time() + self.timeout
        last_log = 0.0

        while time.time() < deadline:
            try:
                resp = self._session.get(
                    f"{self.base}/history/{prompt_id}", timeout=20
                )
                resp.raise_for_status()
                hist = resp.json() or {}
            except requests.RequestException as exc:
                LOG.warning("查询进度失败（稍后重试）: %s", exc)
                time.sleep(self.poll_interval)
                continue

            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {}) or {}
                if status.get("completed") or status.get("status_str") == "success":
                    LOG.info("任务完成 prompt_id=%s", prompt_id)
                    return entry
                if status.get("status_str") == "error":
                    raise ComfyError(
                        f"ComfyUI 执行报错:\n{self._extract_error(status)}"
                    )

            now = time.time()
            if now - last_log > 30:
                remain = int(deadline - now)
                LOG.info("等待生成中... 剩余超时 %s 秒", remain)
                last_log = now
            time.sleep(self.poll_interval)

        raise ComfyError(f"等待超时（{self.timeout}秒），任务可能还在队列里")

    @staticmethod
    def _extract_error(status: dict[str, Any]) -> str:
        """从 status.messages 里挖出可读的错误."""
        lines: list[str] = []
        for msg in status.get("messages", []) or []:
            if not isinstance(msg, (list, tuple)) or len(msg) < 2:
                continue
            kind, body = msg[0], msg[1]
            if kind in ("execution_error", "execution_interrupted") and isinstance(
                body, dict
            ):
                lines.append(
                    f"  节点 {body.get('node_id')} ({body.get('node_type')}): "
                    f"{body.get('exception_type')} - {body.get('exception_message')}"
                )
        return "\n".join(lines) if lines else json.dumps(status, ensure_ascii=False)[:800]

    def locate_video(self, entry: dict[str, Any], since: float) -> Path:
        """从 history 结果里定位产出的视频文件."""
        candidates: list[str] = []

        for node_id, out in (entry.get("outputs") or {}).items():
            if not isinstance(out, dict):
                continue
            for key, items in out.items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    fname = it.get("filename")
                    if not fname:
                        continue
                    ext = Path(str(fname)).suffix.lower()
                    if ext not in VIDEO_EXTS:
                        continue
                    sub = it.get("subfolder") or ""
                    rel = f"{sub}/{fname}" if sub else str(fname)
                    candidates.append(rel)
                    LOG.debug("history 产出: 节点%s.%s -> %s", node_id, key, rel)

        for rel in candidates:
            full = self.output_dir / rel
            if full.exists():
                LOG.info("定位到视频: %s", full)
                return full

        # 兜底：history 没给出可用路径时，扫输出目录里任务开始后新增的视频
        LOG.warning("history 未给出可用视频路径，回退到扫描输出目录")
        newest = self._newest_video(since)
        if newest:
            LOG.info("扫描定位到视频: %s", newest)
            return newest

        raise ComfyError(
            "任务完成但找不到输出视频。\n"
            f"  history 候选: {candidates or '（空）'}\n"
            f"  输出目录: {self.output_dir}\n"
            "  >> 检查工作流里保存视频的节点，以及 config.yaml 的 comfyui.output_dir"
        )

    def _newest_video(self, since: float) -> Path | None:
        """找输出目录中 since 之后新增的最新视频."""
        if not self.output_dir.exists():
            return None
        best: Path | None = None
        best_mtime = since - 1
        for p in self.output_dir.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt > best_mtime:
                best, best_mtime = p, mt
        return best

    def generate(
        self,
        prompt: str,
        negative: str = "",
        seed: int | None = None,
        ratio: str | None = None,
        duration: int | None = None,
        resolution: str | None = None,
    ) -> Path:
        """一步到位：提交并等待，返回视频路径."""
        started = time.time()
        wf = self.build_prompt(prompt, negative, seed, ratio, duration, resolution)
        pid = self.submit(wf)
        entry = self.wait(pid)
        # 留 1 秒余量，避免文件系统时间精度导致漏判
        return self.locate_video(entry, started - 1)
