"""运行状态与单实例锁."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

MAX_HISTORY = 500


class LockBusy(Exception):
    """已有另一个实例在跑."""


class SingleInstanceLock:
    """基于 PID 文件的单实例锁.

    Windows 上不用 fcntl，改成写 PID + 存活检测：
    锁文件存在但进程已死（比如上次被强杀），自动接管。
    """

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self.acquired = False

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        except Exception:
            return False
        return True

    def acquire(self) -> None:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_file.exists():
            try:
                old = int(self.lock_file.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                old = 0
            if old and old != os.getpid() and self._alive(old):
                raise LockBusy(
                    f"已有实例在运行（PID {old}）。"
                    f"如果确认它已经死了，删掉 {self.lock_file} 再启动。"
                )
            LOG.warning("发现残留锁文件（PID %s 已不存在），自动接管", old)

        self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.lock_file.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            LOG.debug("释放锁失败: %s", exc)
        self.acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


class StateStore:
    """记录已处理过的 record_id，防止飞书回写失败导致重复生成."""

    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self._data: dict[str, Any] = {"processed": [], "failed": {}, "sources": {}}
        self.load()

    def load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data["processed"] = list(raw.get("processed") or [])
                self._data["failed"] = dict(raw.get("failed") or {})
                # 断点续跑：已成功生成、但后续步骤（超分/投递）失败时的 480P 原片路径
                self._data["sources"] = dict(raw.get("sources") or {})
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("状态文件读取失败，按空状态继续: %s", exc)

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # 只留最近 N 条，避免文件无限膨胀
        self._data["processed"] = self._data["processed"][-MAX_HISTORY:]
        tmp = self.state_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.state_file)
        except OSError as exc:
            LOG.warning("状态文件写入失败: %s", exc)

    def is_done(self, record_id: str) -> bool:
        return record_id in self._data["processed"]

    def mark_done(self, record_id: str) -> None:
        if record_id not in self._data["processed"]:
            self._data["processed"].append(record_id)
        self._data["failed"].pop(record_id, None)
        self.save()

    def fail_count(self, record_id: str) -> int:
        return int(self._data["failed"].get(record_id, 0))

    def mark_failed(self, record_id: str) -> int:
        n = self.fail_count(record_id) + 1
        self._data["failed"][record_id] = n
        self.save()
        return n

    # ---------------- 断点续跑（生成的 480P 原片） ----------------

    def set_source(self, record_id: str, path: str) -> None:
        """记录某条记录已成功生成的 480P 原片路径，供重试时复用."""
        self._data.setdefault("sources", {})[record_id] = path
        self.save()

    def get_source(self, record_id: str) -> str | None:
        return self._data.get("sources", {}).get(record_id)

    def clear_source(self, record_id: str) -> None:
        """整条成功或彻底失败后清除断点，下次重新生成."""
        if self._data.get("sources", {}).pop(record_id, None) is not None:
            self.save()
