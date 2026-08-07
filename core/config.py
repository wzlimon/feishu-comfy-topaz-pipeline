"""配置加载与校验."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger(__name__)

PLACEHOLDER = "[需填写]"

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """配置缺失或非法."""


class Config:
    """点号访问的配置包装."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, path: str, default: Any = None) -> Any:
        """按 'a.b.c' 路径取值."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        """取值并确保不是占位符."""
        val = self.get(path)
        if val is None or val == "":
            raise ConfigError(f"配置项 {path} 未设置")
        if isinstance(val, str) and val.startswith(PLACEHOLDER):
            raise ConfigError(f"配置项 {path} 还是占位符，请填入真实值")
        return val

    def path(self, key_path: str, default: str | None = None) -> Path:
        """取路径型配置，相对路径基于项目根目录解析."""
        raw = self.get(key_path, default)
        if not raw:
            raise ConfigError(f"路径配置 {key_path} 为空")
        p = Path(str(raw))
        return p if p.is_absolute() else (BASE_DIR / p)

    @property
    def raw(self) -> dict[str, Any]:
        return self._data


def load_config(config_file: str | Path | None = None) -> Config:
    """读取 config.yaml."""
    cfg_path = Path(config_file) if config_file else (BASE_DIR / "config.yaml")
    if not cfg_path.is_absolute():
        cfg_path = BASE_DIR / cfg_path
    if not cfg_path.exists():
        raise ConfigError(f"找不到配置文件: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    LOG.debug("已加载配置: %s", cfg_path)
    return Config(data)


def setup_logging(cfg: Config) -> Path:
    """配置日志，同时输出到控制台和按天切分的文件."""
    from logging.handlers import TimedRotatingFileHandler

    log_dir = cfg.path("runtime.log_dir", "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pipeline.log"

    level_name = str(cfg.get("runtime.log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    keep_days = int(cfg.get("runtime.log_keep_days", 14))
    fileh = TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=keep_days, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    fileh.setLevel(level)
    root.addHandler(fileh)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file
