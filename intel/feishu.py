"""飞书对接：两种机器人形态，一套调用接口。

  webhook（自定义机器人）—— 建群 → 加机器人 → 拿 webhook 地址。零权限申请，
      适合个人；缺点是只能发到那一个群，且必须配签名（新版默认开启）。
  app（企业自建应用）—— app_id/app_secret 换 tenant_access_token，可以主动发消息、
      可以列群、可以后续接事件回调。适合团队与多目标。

签名算法（自定义机器人）：以 `{timestamp}\n{secret}` 为 key、空串为 body 做
HmacSHA256，再 base64。时间戳与服务器相差超过 1 小时会被拒。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
from typing import Any

from .util import http_get_text, http_post_json

TOKEN_TTL_FALLBACK = 3600
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


class FeishuError(RuntimeError):
    pass


def _env(section: dict[str, Any], key: str) -> str:
    name = section.get(key) or ""
    return os.environ.get(name, "").strip() if name else ""


def sign(secret: str, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp or int(time.time()))
    key = f"{ts}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return {"timestamp": ts, "sign": base64.b64encode(digest).decode("utf-8")}


def _section(cfg: dict[str, Any]) -> dict[str, Any]:
    return (cfg.get("channels") or {}).get("feishu") or {}


def configured(cfg: dict[str, Any]) -> bool:
    feishu = _section(cfg)
    if not feishu.get("enabled"):
        return False
    if str(feishu.get("mode") or "webhook") == "app":
        return bool(_env(feishu, "app_id_env") and _env(feishu, "app_secret_env"))
    return bool(_env(feishu, "webhook_env"))


def tenant_token(cfg: dict[str, Any], force: bool = False) -> str:
    feishu = _section(cfg)
    app_id = _env(feishu, "app_id_env")
    secret = _env(feishu, "app_secret_env")
    if not app_id or not secret:
        raise FeishuError("缺少 app_id / app_secret 环境变量")
    cached = _TOKEN_CACHE.get(app_id)
    if cached and not force and cached[1] > time.time() + 60:
        return cached[0]

    base = (feishu.get("api_base") or "https://open.feishu.cn").rstrip("/")
    data = http_post_json(f"{base}/open-apis/auth/v3/tenant_access_token/internal",
                          {"app_id": app_id, "app_secret": secret}, timeout=20)
    if int(data.get("code") or -1) != 0:
        raise FeishuError(f"换取 tenant_access_token 失败：{data.get('msg') or data}")
    token = str(data.get("tenant_access_token") or "")
    ttl = int(data.get("expire") or TOKEN_TTL_FALLBACK)
    if not token:
        raise FeishuError("tenant_access_token 为空")
    _TOKEN_CACHE[app_id] = (token, time.time() + ttl)
    return token


def _api(cfg: dict[str, Any], path: str, payload: dict[str, Any] | None = None,
         method: str = "POST") -> dict[str, Any]:
    feishu = _section(cfg)
    base = (feishu.get("api_base") or "https://open.feishu.cn").rstrip("/")
    token = tenant_token(cfg)
    if method == "GET":
        url = f"{base}{path}"
        text = http_get_text(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
        data = json.loads(text)
    else:
        data = http_post_json(f"{base}{path}", payload or {},
                              headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if int(data.get("code") or -1) != 0:
        raise FeishuError(f"{path} 失败：{data.get('msg') or data}")
    return data


def list_chats(cfg: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    """列出机器人所在的群。app 模式专用——webhook 模式没有这个能力。"""
    data = _api(cfg, f"/open-apis/im/v1/chats?page_size={max(1, min(100, limit))}", method="GET")
    items = ((data.get("data") or {}).get("items") or [])
    return [{"chat_id": it.get("chat_id"), "name": it.get("name"),
             "description": it.get("description"), "owner": it.get("owner_id")}
            for it in items]


def send_card(cfg: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    """发送交互卡片。card 为 render.feishu_card() 的返回值。"""
    feishu = _section(cfg)
    mode = str(feishu.get("mode") or "webhook")
    body_card = card.get("card") if isinstance(card, dict) and "card" in card else card

    if mode == "app":
        receive_id = str(feishu.get("receive_id") or "").strip()
        if not receive_id:
            raise FeishuError("app 模式缺少 channels.feishu.receive_id")
        id_type = str(feishu.get("receive_id_type") or "chat_id")
        data = _api(cfg, f"/open-apis/im/v1/messages?receive_id_type={urllib.parse.quote(id_type)}", {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(body_card, ensure_ascii=False),
        })
        return {"ok": True, "detail": f"message_id={(data.get('data') or {}).get('message_id')}"}

    hook = _env(feishu, "webhook_env")
    if not hook:
        raise FeishuError("缺少 webhook 环境变量")
    payload: dict[str, Any] = {"msg_type": "interactive", "card": body_card}
    secret = _env(feishu, "secret_env")
    if secret:
        payload.update(sign(secret))
    resp = http_post_json(hook, payload, timeout=20)
    code = resp.get("code", resp.get("StatusCode", 0))
    if code not in (0, None):
        raise FeishuError(f"webhook 返回 {code}: {resp.get('msg')}")
    return {"ok": True, "detail": "webhook ok"}


def send_text(cfg: dict[str, Any], text: str) -> dict[str, Any]:
    feishu = _section(cfg)
    mode = str(feishu.get("mode") or "webhook")
    if mode == "app":
        receive_id = str(feishu.get("receive_id") or "").strip()
        if not receive_id:
            raise FeishuError("app 模式缺少 channels.feishu.receive_id")
        id_type = str(feishu.get("receive_id_type") or "chat_id")
        _api(cfg, f"/open-apis/im/v1/messages?receive_id_type={urllib.parse.quote(id_type)}", {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        })
        return {"ok": True, "detail": "app text ok"}

    hook = _env(feishu, "webhook_env")
    if not hook:
        raise FeishuError("缺少 webhook 环境变量")
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    secret = _env(feishu, "secret_env")
    if secret:
        payload.update(sign(secret))
    resp = http_post_json(hook, payload, timeout=20)
    code = resp.get("code", resp.get("StatusCode", 0))
    if code not in (0, None):
        raise FeishuError(f"webhook 返回 {code}: {resp.get('msg')}")
    return {"ok": True, "detail": "webhook ok"}
