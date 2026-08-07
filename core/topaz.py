"""Topaz Video AI 命令行超分.

本机实测要点（Topaz Video AI 5.0.2 便携版）：
  1. 必须用 Topaz 自带的 ffmpeg.exe，它编译时带了 --enable-tvai
  2. TVAI_MODEL_DIR / TVAI_MODEL_DATA_DIR 要指向 models 子目录，
     指到上一层会报 "Model not found: prob-4"
  3. tvai_up 的 w/h 只是期望值，实际受模型固定倍数约束
     （864x480 用 prob-4 出来是 1728x960），后面必须再接一级 scale
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config

LOG = logging.getLogger(__name__)

FRAME_RE = re.compile(r"frame=\s*(\d+)")
SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


class TopazError(Exception):
    """超分失败."""


class TopazUpscaler:
    """Topaz Video AI 超分封装."""

    def __init__(self, cfg: Config) -> None:
        self.enabled = bool(cfg.get("topaz.enabled", True))
        self.ffmpeg = Path(str(cfg.get("topaz.ffmpeg", "")))
        self.ffprobe = Path(str(cfg.get("topaz.ffprobe", "")))
        self.model_dir = Path(str(cfg.get("topaz.model_dir", "")))

        self.model = str(cfg.get("topaz.model", "prob-4"))
        self.width = int(cfg.get("topaz.target_width", 1920))
        self.height = int(cfg.get("topaz.target_height", 1080))
        self.aspect_mode = str(cfg.get("topaz.aspect_mode", "stretch")).lower()

        self.encoder = str(cfg.get("topaz.encoder", "h264_nvenc"))
        self.bitrate = str(cfg.get("topaz.bitrate", "12M"))
        self.device = int(cfg.get("topaz.device", 0))
        self.vram = cfg.get("topaz.vram", 1)
        self.instances = int(cfg.get("topaz.instances", 1))
        self.timeout = int(cfg.get("topaz.timeout", 1800))
        # AI 模型(tvai_up)单次静默超时：超过此秒数无任何输出即视为卡死强杀，
        # 然后回退到纯缩放，保证每条都出 1080P 级成品（不会无限挂起流水线）
        self.tvai_timeout = int(cfg.get("topaz.tvai_timeout", 120))

    # ---------------- 环境 ----------------

    def _env(self) -> dict[str, str]:
        """构造运行环境.

        把 Topaz 目录放到 PATH 最前面，确保加载的是它自己那套 dll，
        不会被系统里其他 ffmpeg 抢走。
        """
        env = os.environ.copy()
        env["TVAI_MODEL_DIR"] = str(self.model_dir)
        env["TVAI_MODEL_DATA_DIR"] = str(self.model_dir)
        topaz_bin = str(self.ffmpeg.parent)
        env["PATH"] = topaz_bin + os.pathsep + env.get("PATH", "")
        return env

    def check(self) -> dict[str, Any]:
        """自检：可执行文件、模型目录、tvai 滤镜是否就位."""
        problems: list[str] = []

        if not self.ffmpeg.exists():
            problems.append(f"找不到 Topaz ffmpeg: {self.ffmpeg}")
        if not self.model_dir.exists():
            problems.append(f"找不到模型目录: {self.model_dir}")
        else:
            model_json = self.model_dir / f"{self.model}.json"
            if not model_json.exists():
                available = sorted(
                    p.stem for p in self.model_dir.glob("*.json")
                )[:30]
                problems.append(
                    f"模型 {self.model} 不存在（缺 {model_json.name}）。"
                    f"可用: {', '.join(available)}"
                )

        has_filter = False
        if self.ffmpeg.exists():
            try:
                res = subprocess.run(
                    [str(self.ffmpeg), "-hide_banner", "-filters"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=self._env(),
                    timeout=60,
                )
                has_filter = "tvai_up" in (res.stdout or "")
                if not has_filter:
                    problems.append(
                        "这个 ffmpeg 里没有 tvai_up 滤镜，"
                        "确认用的是 Topaz 安装目录下的 ffmpeg.exe"
                    )
            except (subprocess.SubprocessError, OSError) as exc:
                problems.append(f"调用 ffmpeg 失败: {exc}")

        return {"ok": not problems, "problems": problems, "tvai_up": has_filter}

    def probe(self, video: Path) -> dict[str, Any]:
        """读取视频基本信息."""
        if not self.ffprobe.exists():
            return {}
        try:
            res = subprocess.run(
                [
                    str(self.ffprobe),
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,r_frame_rate,duration",
                    "-of", "default=noprint_wrappers=1:nokey=0",
                    str(video),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._env(),
                timeout=60,
            )
            info: dict[str, Any] = {}
            for line in (res.stdout or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            return info
        except (subprocess.SubprocessError, OSError):
            return {}

    # ---------------- 滤镜 ----------------

    def _filter_chain(self, width: int, height: int, use_tvai: bool = True) -> str:
        """构造滤镜链.

        width/height 为目标分辨率（由 _target_size 按源片比例算出，保证不拉伸）。
        use_tvai=True 时第一级用 tvai_up(AI 模型)，否则用普通 lanczos 缩放兜底。
        tvai_up 受模型倍数限制，输出未必正好是目标分辨率，后面再跟一级 scale 校正。
        """
        width = int(width)
        height = int(height)

        if use_tvai:
            first = (
                f"tvai_up=model={self.model}:scale=0"
                f":w={width}:h={height}"
                f":device={self.device}:vram={self.vram}:instances={self.instances}"
            )
        else:
            first = f"scale=w={width}:h={height}:flags=lanczos"

        geom = f"scale=w={width}:h={height}:flags=lanczos"
        return f"{first},{geom},scale=out_color_matrix=bt709"

    def _encoder_args(self) -> list[str]:
        args = ["-c:v", self.encoder]
        if "nvenc" in self.encoder:
            args += ["-profile:v", "high", "-preset", "p5", "-rc", "vbr"]
        elif self.encoder in ("libx264", "libx265"):
            args += ["-preset", "medium", "-crf", "18"]
        args += ["-pix_fmt", "yuv420p", "-b:v", self.bitrate]
        return args

    # ---------------- 执行 ----------------

    def _target_size(self, sw: int, sh: int) -> tuple[int, int]:
        """按源片宽高比算 1080P 目标尺寸，保持比例不拉伸、不补黑边。

        竖屏 -> 高 1920；横屏/方形 -> 高 1080；宽取比例对齐到偶数。
        """
        if not sw or not sh:
            return self.width, self.height
        if sh > sw:  # 竖屏
            h = 1920
            w = max(2, round(h * sw / sh / 2) * 2)
        else:        # 横屏或方形
            h = 1080
            w = max(2, round(h * sw / sh / 2) * 2)
        return w, h

    @staticmethod
    def _kill(proc) -> None:
        try:
            proc.kill()
        except Exception:
            pass

    def _run_ffmpeg(
        self,
        src: Path,
        dst: Path,
        tw: int,
        th: int,
        use_tvai: bool,
        timeout: int,
    ) -> bool:
        """跑一次 ffmpeg 超分。成功返回 True；超时/非零退出/输出异常返回 False。

        timeout 为「静默超时」：若 ffmpeg 在 timeout 秒内没有任何输出（含进度行），
        视为卡死并强杀——避免子进程静默挂起导致流水线无限阻塞。
        """
        chain = self._filter_chain(tw, th, use_tvai=use_tvai)
        cmd = [
            str(self.ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i", str(src),
            "-filter_complex", chain,
            *self._encoder_args(),
            "-map_metadata", "-1",
            "-movflags", "+faststart",
            "-c:a", "copy",
            str(dst),
        ]
        LOG.debug("ffmpeg: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        last_activity = [time.time()]
        stop = threading.Event()

        def _watchdog() -> None:
            while not stop.is_set() and proc.poll() is None:
                if time.time() - last_activity[0] > timeout:
                    LOG.warning(
                        "ffmpeg(%s) 静默超过 %s 秒，强杀",
                        "tvai" if use_tvai else "scale",
                        timeout,
                    )
                    self._kill(proc)
                    return
                time.sleep(3)

        watch = threading.Thread(target=_watchdog, daemon=True)
        watch.start()

        tail: list[str] = []
        last_report = 0.0
        started = time.time()
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                last_activity[0] = time.time()
                line = line.rstrip()
                if not line:
                    continue
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
                m = FRAME_RE.search(line)
                if m:
                    now = time.time()
                    if now - last_report > 20:
                        sp = SPEED_RE.search(line)
                        LOG.info(
                            "超分进度: 第 %s 帧%s，已用 %.0f 秒",
                            m.group(1),
                            f"，速度 {sp.group(1)}x" if sp else "",
                            now - started,
                        )
                        last_report = now
            proc.wait(timeout=30)
        except Exception:
            if proc.poll() is None:
                self._kill(proc)
            raise
        finally:
            stop.set()
            watch.join(timeout=5)

        if proc.returncode != 0:
            LOG.warning(
                "ffmpeg 退出码 %s（%s）\n%s",
                proc.returncode,
                "tvai" if use_tvai else "scale",
                "\n".join(tail[-15:]),
            )
            return False
        if not dst.exists() or dst.stat().st_size < 10_000:
            LOG.warning(
                "ffmpeg 输出异常（不存在或过小）\n%s", "\n".join(tail[-15:])
            )
            return False
        return True

    def upscale(self, src: Path, dst: Path) -> Path:
        """把 src 超分到 dst，返回 dst。优先 tvai_up(AI)，失败/超时回退纯缩放。"""
        if not self.enabled:
            raise TopazError("Topaz 超分已在配置里关闭")
        if not src.exists():
            raise TopazError(f"源视频不存在: {src}")

        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()

        info = self.probe(src)
        sw = int(info.get("width") or 0)
        sh = int(info.get("height") or 0)
        tw, th = self._target_size(sw, sh)

        started = time.time()
        LOG.info(
            "超分开始: %s (%sx%s) -> %sx%s，模型 %s",
            src.name,
            sw or "?",
            sh or "?",
            tw,
            th,
            self.model,
        )

        ok = self._run_ffmpeg(src, dst, tw, th, use_tvai=True, timeout=self.tvai_timeout)
        if not ok:
            LOG.warning(
                "tvai_up 未成功，回退到纯高质量缩放（仍输出 %sx%s 成品）", tw, th
            )
            ok = self._run_ffmpeg(
                src, dst, tw, th, use_tvai=False, timeout=self.timeout
            )

        if not ok or not dst.exists() or dst.stat().st_size < 10_000:
            raise TopazError("超分失败：tvai_up 与回退缩放均未产出有效文件")

        out_info = self.probe(dst)
        LOG.info(
            "超分完成: %s (%sx%s, %.1f MB)，耗时 %.0f 秒",
            dst.name,
            out_info.get("width", "?"),
            out_info.get("height", "?"),
            dst.stat().st_size / 1024 / 1024,
            time.time() - started,
        )
        return dst
