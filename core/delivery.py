"""成品投递到百度网盘同步目录.

关键点：先写成 .tmp，完全落盘后再改名成 .mp4。
百度网盘客户端监听目录变化，如果直接写 .mp4，
它会在文件还没写完时就开始上传，云端拿到的是残片。
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from .config import Config

LOG = logging.getLogger(__name__)

# Windows 文件名非法字符，外加控制字符
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


class DeliveryError(Exception):
    """投递失败."""


def safe_name(text: str, max_len: int = 20) -> str:
    """把提示词压成能当文件名的短串."""
    cleaned = ILLEGAL.sub("", text or "").strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = cleaned.strip("._-")
    if not cleaned:
        cleaned = "video"
    return cleaned[:max_len]


class NetdiskDelivery:
    """投递到百度网盘同步目录."""

    def __init__(self, cfg: Config) -> None:
        self.sync_root = Path(str(cfg.get("baidu.sync_root", "")))
        self.subdir = str(cfg.get("baidu.target_subdir", "minimax_video_1080"))
        self.template = str(
            cfg.get("baidu.filename_template", "{date}_{time}_{prompt_short}.mp4")
        )
        self.prompt_len = int(cfg.get("baidu.prompt_short_len", 20))
        self.use_temp = bool(cfg.get("baidu.use_temp_suffix", True))

    @property
    def target_dir(self) -> Path:
        return self.sync_root / self.subdir

    def check(self) -> dict[str, object]:
        """自检同步目录是否可写."""
        problems: list[str] = []
        if not self.sync_root.exists():
            problems.append(
                f"百度网盘同步目录不存在: {self.sync_root}\n"
                "  >> 打开百度网盘客户端 -> 设置 -> 同步盘，"
                "确认本地路径后改 config.yaml 的 baidu.sync_root"
            )
        else:
            try:
                self.target_dir.mkdir(parents=True, exist_ok=True)
                probe = self.target_dir / ".write_test"
                probe.write_text("ok", encoding="utf-8")
                # 沙箱 / 无回收站环境下 unlink 可能被安全策略拦截而抛错。
                # 写探针成功即证明目录可写，删除失败仅仅是清理失败，
                # 不应据此误判为“不可写”。吞掉异常即可。
                try:
                    probe.unlink()
                except Exception as exc:  # noqa: BLE001 - 探针清不掉不算致命
                    LOG.debug("清除探针文件失败（不影响可写性判定）: %s", exc)
            except OSError as exc:
                problems.append(f"同步目录不可写: {exc}")
        return {"ok": not problems, "problems": problems, "dir": str(self.target_dir)}

    def build_filename(self, prompt: str, record_id: str, seq: int = 0) -> str:
        now = datetime.now()
        name = self.template.format(
            date=now.strftime("%Y%m%d"),
            time=now.strftime("%H%M%S"),
            record_id=(record_id or "")[-8:],
            seq=f"{seq:03d}",
            prompt_short=safe_name(prompt, self.prompt_len),
        )
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
        return ILLEGAL.sub("", name)

    def deliver(self, src: Path, filename: str) -> Path:
        """把成品放进同步目录，返回最终路径."""
        if not src.exists():
            raise DeliveryError(f"待投递文件不存在: {src}")

        self.target_dir.mkdir(parents=True, exist_ok=True)
        final = self.target_dir / filename

        # 重名就加序号，不覆盖已有成品
        if final.exists():
            stem, suffix = final.stem, final.suffix
            for i in range(1, 1000):
                cand = self.target_dir / f"{stem}_{i}{suffix}"
                if not cand.exists():
                    final = cand
                    break

        if self.use_temp:
            tmp = final.with_suffix(final.suffix + ".tmp")
            if tmp.exists():
                tmp.unlink()
            LOG.info("写入临时文件: %s", tmp.name)
            shutil.copy2(src, tmp)
            # 确认大小一致再改名，防止拷贝被截断
            if tmp.stat().st_size != src.stat().st_size:
                tmp.unlink(missing_ok=True)
                raise DeliveryError("拷贝后大小不一致，已中止")
            time.sleep(0.5)
            tmp.rename(final)
            LOG.info("改名完成，百度网盘开始上传: %s", final.name)
        else:
            shutil.copy2(src, final)
            LOG.info("已写入: %s", final.name)

        size_mb = final.stat().st_size / 1024 / 1024
        LOG.info("成品就位: %s (%.1f MB)", final, size_mb)
        return final

    def netdisk_path(self, final: Path) -> str:
        """给飞书回写用的、人类可读的网盘内路径."""
        try:
            rel = final.relative_to(self.sync_root)
            return f"/{rel.as_posix()}"
        except ValueError:
            return str(final)
