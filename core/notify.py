"""完成通知：视频生成/超分/投递完成后，向飞书群机器人推送卡片消息.

为什么用「群机器人 webhook」而不是「应用发消息」：
  - 零权限申请：建个群 -> 加自定义机器人 -> 复制 webhook 即可，不用去
    开放平台开 im:message 权限、也不用配可见范围/拿 user_id。
  - 独立通道：即使飞书多维表格接口抖动，通知仍然能发（走另一条 webhook）。
  - 手机即时收到：群消息在飞书 App 里实时推送。

（可选）开启机器人的「签名校验」后，把签名密钥填进 config 的
feishu_webhook_secret，发送时会自动带上 timestamp + sign。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any

import requests

from .config import Config

LOG = logging.getLogger("notify")


class Notifier:
    """统一通知入口，目前只实现飞书群机器人 webhook."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.enabled = bool(cfg.get("notify.enabled", False))
        self.on = (cfg.get("notify.on", "both") or "both").lower()
        self.channel = (
            cfg.get("notify.channel", "feishu_webhook") or "feishu_webhook"
        ).lower()
        self.webhook_url = cfg.get("notify.feishu_webhook_url", "") or ""
        self.secret = cfg.get("notify.feishu_webhook_secret", "") or ""
        self._session = requests.Session()

    @property
    def available(self) -> bool:
        """通知是否真正可用（开启 + 渠道支持 + webhook 已填）."""
        if not self.enabled:
            return False
        if self.channel != "feishu_webhook":
            return False
        return bool(self.webhook_url) and not str(self.webhook_url).startswith(
            "[需填写]"
        )

    # ---------------- 对外接口 ----------------

    def notify_success(
        self,
        *,
        prompt: str,
        ratio: str,
        video_len: int | None,
        netdisk_path: str,
        duration: float,
        filename: str,
        resolution: str = "",
        upscale: str = "",
    ) -> None:
        if not self.available or self.on not in ("both", "done"):
            return

        elements: list[dict[str, Any]] = [
            self._div(f"**文件**：{filename}"),
            self._div(f"**提示词**：{prompt}"),
        ]
        if ratio:
            elements.append(self._div(f"**比例**：{ratio}"))
        if video_len:
            elements.append(self._div(f"**时长**：{video_len} 秒"))
        if resolution:
            elements.append(self._div(f"**分辨率**：{resolution}"))
        if upscale:
            elements.append(self._div(f"**超分**：{upscale}"))
        elements.append(self._div(f"**网盘路径**：{netdisk_path}"))
        elements.append(
            self._div(f"**总耗时**：{duration:.0f} 秒（{duration / 60:.1f} 分钟）")
        )

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "🎬 视频生成完成"},
                "template": "green",
            },
            "elements": elements,
        }
        self._send(card)

    def notify_failure(
        self, *, prompt: str, reason: str, record_id: str
    ) -> None:
        if not self.available or self.on not in ("both", "failed"):
            return

        elements = [
            self._div(f"**记录 ID**：{record_id}"),
            self._div(f"**提示词**：{prompt}"),
            self._div(f"**失败原因**：{reason}"),
        ]
        card = {
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ 视频生成失败"},
                "template": "red",
            },
            "elements": elements,
        }
        self._send(card)

    # ---------------- 内部 ----------------

    @staticmethod
    def _div(text: str) -> dict[str, Any]:
        return {"tag": "div", "text": {"tag": "lark_md", "content": text}}

    def _send(self, card: dict[str, Any]) -> None:
        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {"config": {"wide_screen_mode": True}, **card},
        }
        if self.secret:
            ts = str(int(time.time()))
            payload["timestamp"] = ts
            payload["sign"] = self._sign(ts, self.secret)

        try:
            resp = self._session.post(self.webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code", 0) != 0:
                LOG.warning(
                    "飞书通知发送失败 code=%s msg=%s",
                    data.get("code"),
                    data.get("msg"),
                )
            else:
                LOG.info("飞书通知已发送")
        except Exception as exc:  # noqa: BLE001
            # 通知失败绝不能影响主流程
            LOG.warning("飞书通知发送异常（不影响主流程）: %s", exc)

    @staticmethod
    def _sign(timestamp: str, secret: str) -> str:
        string_to_sign = f"{timestamp}\n{secret}"
        h = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(h).decode("utf-8")
