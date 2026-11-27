"""投递：渠道分流 + 幂等 + 失败隔离。

分流不是「同一份内容发三个地方」，而是不同置信度走不同节奏：
  immediate (>=0.80) —— Telegram，单条，立刻
  daily    (0.50-0.80) —— 飞书卡片 / 钉钉 markdown / Telegram 合并，日报
  archive  (<0.50)    —— 只入库，等控制台反馈或周报

三个通道都只负责「送出去」：消息里没有任何按钮，反馈一律回控制台。
webhook 机器人本来也收不到卡片回调，留个按不动的按钮只是自欺。

幂等键是 canonical_url + channel，30 天内不重发。这一层必须存在：
即使上游水位线失效、脚本被手动重跑两次，用户也不会收到重复推送。
"""

from __future__ import annotations

import os
import time
from typing import Any

from . import dingtalk, feishu, render
from .i18n import resolve_lang, t
from .models import CHANNEL_DAILY, CHANNEL_IMMEDIATE, Item
from .util import http_post_json

IDEMPOTENT_DAYS = 30
RETRIES = 2


def _env(section: dict[str, Any], key: str) -> str:
    name = section.get(key) or ""
    return os.environ.get(name, "").strip() if name else ""


def _result(channel: str, ok: bool, detail: str = "", count: int = 0) -> dict[str, Any]:
    return {"channel": channel, "ok": ok, "detail": str(detail)[:200], "count": count}


def _post(url: str, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            return http_post_json(url, payload, timeout=timeout)
        except Exception as exc:
            last = exc
            if attempt < RETRIES:
                time.sleep(0.6 * (2 ** attempt))
    raise RuntimeError(f"POST 失败（{RETRIES + 1} 次）：{last}")


def _fresh(store: Any, item: Item) -> bool:
    if not item.canonical_url:
        return True
    return not store.pushed_within(item.canonical_url, IDEMPOTENT_DAYS * 24)


def dispatch(
    cfg: dict[str, Any],
    items: list[Item],
    store: Any,
    run_id: str,
    title: str,
    tracer: Any = None,
    pack: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    channels = cfg["channels"]
    lang = resolve_lang(cfg)
    console_url = str((cfg.get("ui") or {}).get("review_url") or "")

    immediate = [it for it in items if it.channel == CHANNEL_IMMEDIATE and _fresh(store, it)]
    daily = [it for it in items if it.channel == CHANNEL_DAILY and _fresh(store, it)]
    enabled_any = any(channels.get(k, {}).get("enabled")
                      for k in ("feishu", "dingtalk", "telegram"))

    if not enabled_any:
        return [_result("none", True, "所有渠道关闭，仅落库与生成 HTML")]

    # ---------- 飞书：日报主通道，承载 immediate + daily ----------
    feishu_cfg = channels.get("feishu", {})
    if feishu_cfg.get("enabled"):
        batch = daily + immediate
        if not feishu.configured(cfg):
            env_hint = ("app_id/app_secret" if str(feishu_cfg.get("mode") or "webhook") == "app"
                        else feishu_cfg.get("webhook_env"))
            results.append(_result("feishu", False, f"飞书凭据未设置：{env_hint}"))
        elif not batch:
            results.append(_result("feishu", True, t("skipped_empty", lang)))
        else:
            try:
                out = feishu.send_card(cfg, render.feishu_card(batch, title, lang,
                                                              pack=pack, tasks=tasks,
                                                              console_url=console_url))
                results.append(_result("feishu", True, out.get("detail", ""), len(batch)))
                store.mark_pushed([it.canonical_url for it in batch if it.canonical_url],
                                  "feishu", run_id)
            except Exception as exc:
                results.append(_result("feishu", False, exc))

    # ---------- 钉钉：webhook 合并成一条 markdown，一轮只发一次 ----------
    # 钉钉限流 20 条/分钟，超了会被静默丢 10 分钟。所以这里不按条发，
    # 整批拼成一条消息（超长才分段），既省额度也不会因刷屏被限。
    dd = channels.get("dingtalk", {})
    if dd.get("enabled"):
        batch = daily + immediate
        if not dingtalk.configured(cfg):
            results.append(_result("dingtalk", False,
                                   f"钉钉凭据未设置：{dd.get('webhook_env') or 'DINGTALK_WEBHOOK'}"))
        elif not batch:
            results.append(_result("dingtalk", True, t("skipped_empty", lang)))
        else:
            sent = 0
            try:
                for msg in render.dingtalk_markdown(batch, title, lang,
                                                    pack=pack, tasks=tasks,
                                                    console_url=console_url):
                    dingtalk.send_markdown(cfg, msg["title"], msg["text"])
                    sent += 1
                results.append(_result("dingtalk", True, f"{sent} 段", len(batch)))
                store.mark_pushed([it.canonical_url for it in batch if it.canonical_url],
                                  "dingtalk", run_id)
            except Exception as exc:
                results.append(_result("dingtalk", False, exc, sent))

    # ---------- Telegram：即时通道，一条事件一条消息 ----------
    tg = channels.get("telegram", {})
    if tg.get("enabled"):
        token, chat_id = _env(tg, "bot_token_env"), _env(tg, "chat_id_env")
        batch = immediate or daily[:3]
        if not token or not chat_id:
            results.append(_result("telegram", False, "bot_token / chat_id 环境变量未设置"))
        elif not batch:
            results.append(_result("telegram", True, "本轮无新增条目，跳过发送"))
        else:
            sent = 0
            try:
                for chunk in render.telegram_chunks(batch, title, lang=lang):
                    _post(f"https://api.telegram.org/bot{token}/sendMessage", {
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    })
                    sent += 1
                results.append(_result("telegram", True, f"{sent} 段", len(batch)))
                store.mark_pushed([it.canonical_url for it in batch if it.canonical_url],
                                  "telegram", run_id)
            except Exception as exc:
                results.append(_result("telegram", False, exc, sent))

    return results


def send_heartbeat(cfg: dict[str, Any], text: str, tracer: Any = None) -> list[dict[str, Any]]:
    """零产出心跳：走最轻的已启用渠道，只发一句话。"""
    results: list[dict[str, Any]] = []
    channels = cfg["channels"]
    lang = resolve_lang(cfg)

    tg = channels.get("telegram", {})
    if tg.get("enabled") and _env(tg, "bot_token_env") and _env(tg, "chat_id_env"):
        try:
            _post(f"https://api.telegram.org/bot{_env(tg, 'bot_token_env')}/sendMessage",
                  {"chat_id": _env(tg, "chat_id_env"), "text": text,
                   "disable_web_page_preview": True})
            results.append(_result("telegram", True, "heartbeat", 1))
        except Exception as exc:
            results.append(_result("telegram", False, exc))

    feishu_cfg = channels.get("feishu", {})
    if feishu_cfg.get("enabled") and feishu.configured(cfg):
        try:
            feishu.send_text(cfg, f"{t('heartbeat_title', lang)}\n{text}")
            results.append(_result("feishu", True, "heartbeat", 1))
        except Exception as exc:
            results.append(_result("feishu", False, exc))

    dd = channels.get("dingtalk", {})
    if dd.get("enabled") and dingtalk.configured(cfg):
        try:
            dingtalk.send_markdown(cfg, t("heartbeat_title", lang),
                                   f"### {t('heartbeat_title', lang)}\n\n{text}")
            results.append(_result("dingtalk", True, "heartbeat", 1))
        except Exception as exc:
            results.append(_result("dingtalk", False, exc))

    if not results:
        results.append(_result("none", True, "无可用渠道，心跳未发送"))
    return results
