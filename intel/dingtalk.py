"""钉钉自定义机器人：webhook + 加签。

协议跟飞书 webhook 几乎一样（`msgtype` + `content`），差别只有三点：
  1. 签名拼在 URL query 上（timestamp & sign），不在 body 里；
  2. markdown 是钉钉自己的子集，没有卡片元素，日报只能由一条消息承载；
  3. 限流 20 条/分钟，超了会被丢掉 10 分钟 —— 所以一轮只发一条合并消息。

加签算法（官方）：HmacSHA256(secret, f"{timestamp}\\n{secret}") → base64 → urlEncode。
时间戳是毫秒，允许误差 ±1 小时，所以本机时钟要准。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from typing import Any

from .util import http_post_json


class DingTalkError(RuntimeError):
    pass


def _section(cfg: dict[str, Any]) -> dict[str, Any]:
    return (cfg.get("channels") or {}).get("dingtalk") or {}


def _env(section: dict[str, Any], key: str) -> str:
    name = str(section.get(key) or "")
    return os.environ.get(name, "").strip() if name else ""


def _mask(url: str) -> str:
    """access_token 是 webhook 的唯一凭证，错误信息里不能原样带出去。"""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    for key in ("access_token", "sign"):
        if key in query:
            value = query[key][0]
            query[key] = [value[:4] + "***" if value else "***"]
    return urllib.parse.urlunsplit(parts._replace(
        query=urllib.parse.urlencode(query, doseq=True)))


def sign(secret: str, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time() * 1000))
    digest = hmac.new(
        secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"), hashlib.sha256
    ).digest()
    return {"timestamp": ts, "sign": urllib.parse.quote_plus(base64.b64encode(digest))}


def configured(cfg: dict[str, Any]) -> bool:
    section = _section(cfg)
    return bool(section.get("enabled") and _env(section, "webhook_env"))


def _url(cfg: dict[str, Any]) -> str:
    section = _section(cfg)
    url = _env(section, "webhook_env")
    if not url:
        raise DingTalkError(
            f"缺少环境变量 {section.get('webhook_env') or 'DINGTALK_WEBHOOK'}")
    secret = _env(section, "secret_env")
    if secret:
        # webhook 地址自带 ?access_token=…，追加参数只能用 &
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(sign(secret))}"
    return url


def _send(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    url = _url(cfg)
    try:
        data = http_post_json(url, payload, timeout=15)
    except Exception as exc:  # noqa: BLE001 —— 统一转成可读错误，顺便给 token 打码
        raise DingTalkError(str(exc).replace(url, _mask(url))) from exc
    code = data.get("errcode", data.get("code", 0))
    if code not in (0, None):
        raise DingTalkError(f"钉钉返回 {code}: {data.get('errmsg') or data}")
    return data


def send_markdown(cfg: dict[str, Any], title: str, text: str) -> dict[str, Any]:
    """标题只用于通知栏与会话列表，钉钉会截到 20 字左右，这里先截一次。"""
    return _send(cfg, {
        "msgtype": "markdown",
        "markdown": {"title": title[:32], "text": text},
    })


def send_text(cfg: dict[str, Any], text: str) -> dict[str, Any]:
    return _send(cfg, {"msgtype": "text", "text": {"content": text[:1800]}})
