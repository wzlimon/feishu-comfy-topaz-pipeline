"""飞书多维表格客户端.

只用到三个接口：
  1. 获取 tenant_access_token
  2. 条件查询记录（捞出"待处理"的行）
  3. 更新单条记录（回写状态与结果）
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .config import Config

LOG = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"

# 常见错误码 -> 人话
ERROR_HINTS = {
    99991672: (
        "应用缺少多维表格权限。去开放平台 -> 权限管理，开启 "
        "bitable:app（或「查看、评论、编辑和管理多维表格」），"
        "然后务必到「版本管理与发布」发布新版本才会生效。"
    ),
    91403: (
        "应用没有这张多维表格的访问权限。打开多维表格 -> 右上角「...」 -> "
        "更多 -> 添加文档应用 -> 把你的自建应用加进来，并给「可编辑」权限。"
    ),
    99991663: "app_id 或 app_secret 不正确，请核对开放平台「凭证与基础信息」。",
    1254005: "table_id 不存在，请核对多维表格 URL 里 ?table= 后面那段。",
    1254040: "app_token 不存在，请核对多维表格 URL 里 /base/ 后面那段。",
}


class FeishuError(Exception):
    """飞书接口返回了非 0 的 code."""

    def __init__(self, code: int, msg: str) -> None:
        hint = ERROR_HINTS.get(code)
        text = f"飞书接口错误 code={code}: {msg}"
        if hint:
            text += f"\n  >> {hint}"
        super().__init__(text)
        self.code = code


def _plain_text(value: Any) -> str:
    """把多维表格字段值统一转成纯文本.

    文本字段可能返回 str，也可能返回 [{"type":"text","text":"..."}]。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "").strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    return str(value).strip()


class FeishuBitable:
    """多维表格读写."""

    def __init__(self, cfg: Config) -> None:
        # 注意：这里故意不做校验，允许在飞书还没配好的情况下构造出对象，
        # 这样 doctor.py / main.py --dry-run 能先把其它三段检查完。
        # 真正发请求前才由 _ensure_configured() 兜底。
        self._cfg = cfg
        self.app_id = cfg.get("feishu.app_id", "")
        self.app_secret = cfg.get("feishu.app_secret", "")
        self.app_token = cfg.get("feishu.app_token", "")
        self.table_id = cfg.get("feishu.table_id", "")

        self.fields: dict[str, str] = cfg.get("feishu.fields", {}) or {}
        self.status_values: dict[str, str] = cfg.get("feishu.status_values", {}) or {}

        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        """四项凭证是否都已填真实值."""
        for key in ("feishu.app_id", "feishu.app_secret", "feishu.app_token",
                    "feishu.table_id"):
            val = self._cfg.get(key)
            if not val or (isinstance(val, str) and val.startswith("[需填写]")):
                return False
        return True

    def _ensure_configured(self) -> None:
        for key in ("feishu.app_id", "feishu.app_secret", "feishu.app_token",
                    "feishu.table_id"):
            self._cfg.require(key)

    # ---------------- 鉴权 ----------------

    def _get_token(self) -> str:
        """拿 tenant_access_token，提前 5 分钟续期."""
        self._ensure_configured()
        now = time.time()
        if self._token and now < self._token_expire_at - 300:
            return self._token

        url = f"{BASE_URL}/auth/v3/tenant_access_token/internal"
        resp = self._session.post(
            url,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(data.get("code", -1), data.get("msg", "获取 token 失败"))

        self._token = data["tenant_access_token"]
        self._token_expire_at = now + int(data.get("expire", 7200))
        LOG.debug("已获取 tenant_access_token，有效期 %s 秒", data.get("expire"))
        return self._token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """带鉴权的请求，401 时自动重新取 token 重试一次."""
        url = f"{BASE_URL}{path}"

        for attempt in (1, 2):
            headers = {
                "Authorization": f"Bearer {self._get_token()}",
                "Content-Type": "application/json; charset=utf-8",
            }
            resp = self._session.request(
                method, url, headers=headers, timeout=30, **kwargs
            )

            if resp.status_code == 401 and attempt == 1:
                LOG.warning("token 失效，重新获取后重试")
                self._token = None
                continue

            try:
                data = resp.json()
            except ValueError:
                resp.raise_for_status()
                raise FeishuError(-1, f"响应不是 JSON: {resp.text[:200]}")

            if data.get("code") != 0:
                raise FeishuError(data.get("code", -1), data.get("msg", "未知错误"))
            return data.get("data", {})

        raise FeishuError(-1, "请求重试后仍然失败")

    # ---------------- 业务 ----------------

    def fetch_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        """捞出状态为「待处理」的记录，按创建时间正序（先进先出）."""
        status_field = self.fields.get("status", "状态")
        pending = self.status_values.get("pending", "待处理")

        path = (
            f"/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records/search"
        )
        body = {
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {
                        "field_name": status_field,
                        "operator": "is",
                        "value": [pending],
                    }
                ],
            },
            "automatic_fields": True,
        }

        data = self._request(
            "POST", f"{path}?page_size={limit}", json=body
        )
        items = data.get("items", []) or []

        records: list[dict[str, Any]] = []
        for item in items:
            f = item.get("fields", {}) or {}
            records.append(
                {
                    "record_id": item.get("record_id"),
                    "prompt": _plain_text(f.get(self.fields.get("prompt", "提示词"))),
                    "negative": _plain_text(
                        f.get(self.fields.get("negative", "反向提示词"))
                    ),
                    "seed": f.get(self.fields.get("seed", "随机种子")),
                    "ratio": _plain_text(f.get(self.fields.get("ratio", "ratio"))),
                    "video_len": _plain_text(
                        f.get(self.fields.get("video_len", "video_len"))
                    ),
                    "resolution": _plain_text(
                        f.get(self.fields.get("resolution", "resolution"))
                    ),
                    "upscale": _plain_text(
                        f.get(self.fields.get("upscale", "upscale"))
                    ),
                    "created": item.get("created_time") or 0,
                    "_raw": f,
                }
            )

        # 按创建时间正序，保证先提交的先跑
        records.sort(key=lambda r: r.get("created") or 0)
        return records

    def update(self, record_id: str, values: dict[str, Any]) -> None:
        """更新记录字段."""
        path = (
            f"/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records/{record_id}"
        )
        self._request("PUT", path, json={"fields": values})

    def mark_running(self, record_id: str) -> None:
        self.update(
            record_id,
            {
                self.fields.get("status", "状态"): self.status_values.get(
                    "running", "生成中"
                )
            },
        )

    def mark_done(
        self,
        record_id: str,
        *,
        filename: str,
        netdisk_path: str,
        duration: float,
    ) -> None:
        values: dict[str, Any] = {
            self.fields.get("status", "状态"): self.status_values.get("done", "已完成")
        }
        if self.fields.get("result_name"):
            values[self.fields["result_name"]] = filename
        if self.fields.get("result_path"):
            values[self.fields["result_path"]] = netdisk_path
        if self.fields.get("duration"):
            values[self.fields["duration"]] = round(duration, 1)
        if self.fields.get("error"):
            values[self.fields["error"]] = ""
        self.update(record_id, values)

    def mark_failed(self, record_id: str, reason: str) -> None:
        values: dict[str, Any] = {
            self.fields.get("status", "状态"): self.status_values.get("failed", "失败")
        }
        if self.fields.get("error"):
            values[self.fields["error"]] = reason[:2000]
        self.update(record_id, values)

    def ping(self) -> dict[str, Any]:
        """连通性自检：取一条记录，验证 token / 权限 / ID 都对."""
        path = (
            f"/bitable/v1/apps/{self.app_token}"
            f"/tables/{self.table_id}/records?page_size=1"
        )
        data = self._request("GET", path)
        items = data.get("items", []) or []
        field_names: list[str] = []
        if items:
            field_names = list((items[0].get("fields") or {}).keys())
        return {"total": data.get("total", 0), "sample_fields": field_names}

    def list_field_names(self) -> list[str]:
        """列出数据表的所有字段名，用于核对配置里的字段映射."""
        path = f"/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields"
        data = self._request("GET", f"{path}?page_size=100")
        return [it.get("field_name", "") for it in (data.get("items") or [])]
