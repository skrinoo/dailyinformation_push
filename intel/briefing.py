"""晚间简报：一天结束时的兜底。

它解决的不是「信息」，是「不确定感」：白天没收到推送时，用户不知道是
「没有值得看的东西」还是「它坏掉了」。简报把这件事一次讲清——
今天跑了几轮、扫了多少、入选多少、推送几条、静默几轮、建了几条待办。

内容来源全是本地库，不重新抓取、不重新判定、不烧 token：
  runs        —— 每轮的漏斗数字
  topic_items —— 每个关注项今天收过什么（含未推送的）
  tasks_out   —— 今天建了哪些待办

所以简报可以在任何时间点重跑，随时补发，结果只会更完整不会漂移。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import feishu, render
from .i18n import resolve_lang
from .topics import list_topics
from .util import now_local


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _day_window(cfg: dict[str, Any], day: str | None = None) -> tuple[datetime, datetime, str]:
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    now = now_local(tz)
    if day and day not in ("today", "yesterday"):
        parsed = datetime.strptime(day, "%Y-%m-%d")
        start = parsed.replace(tzinfo=now.tzinfo)
    elif day == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1), start.strftime("%Y-%m-%d")


def collect(store: Any, cfg: dict[str, Any], day: str | None = None) -> dict[str, Any]:
    start, end, label = _day_window(cfg, day)
    runs = [r for r in store.runs_between(_iso(start), _iso(end)) if r.get("status") != "running"]
    rows = store.topic_items(_iso(start), _iso(end), only_picked=True, limit=400)
    tasks = store.tasks_between(_iso(start), _iso(end))

    names = {tp["id"]: tp["name"] for tp in list_topics(cfg)}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get("topic") or "", []).append(row)

    topics_out: list[dict[str, Any]] = []
    for tid in [tp["id"] for tp in list_topics(cfg) if tp.get("enabled", True)]:
        items = grouped.get(tid) or []
        # 没有内容的关注项也要列出来：用户需要知道「它盯了，只是没打扰」。
        topics_out.append({
            "id": tid,
            "name": names.get(tid) or tid or "默认",
            "kept": len(items),
            "items": items[:8],
        })
    # 配置里已删除、但今天确实收过的关注项也要露出来，否则用户会以为丢数据
    for tid, items in grouped.items():
        if tid in names:
            continue
        topics_out.append({"id": tid, "name": tid or "默认", "kept": len(items),
                           "items": items[:8]})

    silenced = sum(1 for r in runs if "silenc" in str(r.get("note") or "").lower())
    return {
        "date": label,
        "runs": len(runs),
        "fetched": sum(int(r.get("fetched") or 0) for r in runs),
        # 入选数用收录条目数，而不是 runs.kept 求和：同一个 URL 被两轮都收过时，
        # 求和会把 8 条说成 10 条，用户对照下面的列表会以为丢了内容。
        "kept": len(rows),
        "pushed": sum(int(r.get("pushed") or 0) for r in runs),
        "silenced": silenced,
        "rejected": sum(max(0, int(r.get("fetched") or 0) - int(r.get("kept") or 0))
                        for r in runs),
        "topics": topics_out,
        "tasks": tasks,
        "next": "、".join(names.get(tp["id"]) or tp["id"] for tp in list_topics(cfg)
                         if tp.get("enabled", True) and tp["id"]),
    }


def run_briefing(
    cfg: dict[str, Any],
    store: Any,
    *,
    day: str | None = None,
    dry_run: bool = False,
    push: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    lang = resolve_lang(cfg)
    summary = collect(store, cfg, day=day)
    out_dir = Path(cfg["storage"].get("out_dir", "out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"brief-{summary['date']}.html"
    path.write_text(render.briefing_html(summary, lang), encoding="utf-8")

    result: dict[str, Any] = {"summary": summary, "html": str(path), "push": []}
    if dry_run or not push:
        result["push"] = [{"channel": "dry-run", "ok": True, "detail": "未实际发送"}]
    elif not ((cfg.get("channels") or {}).get("feishu") or {}).get("enabled"):
        result["push"] = [{"channel": "none", "ok": True, "detail": "飞书未启用，仅生成文件"}]
    elif not feishu.configured(cfg):
        result["push"] = [{"channel": "feishu", "ok": False, "detail": "飞书凭据未设置"}]
    else:
        try:
            feishu.send_card(cfg, render.briefing_card(summary, lang))
            result["push"] = [{"channel": "feishu", "ok": True, "detail": "briefing card"}]
        except Exception as exc:
            result["push"] = [{"channel": "feishu", "ok": False, "detail": str(exc)[:200]}]

    if verbose:
        s = summary
        print(f"简报 {s['date']}：{s['runs']} 轮 · 扫描 {s['fetched']} · 入选 {s['kept']}"
              f" · 推送 {s['pushed']} · 静默 {s['silenced']} · 待办 {len(s['tasks'])}")
        for topic in s["topics"]:
            print(f"  {topic['name']:<12} {topic['kept']} 条")
        print(f"报告 {path}")
        for p in result["push"]:
            print(f"投递 [{'OK ' if p.get('ok') else 'ERR'}] {p['channel']}: {p['detail'][:60]}")
    return result
