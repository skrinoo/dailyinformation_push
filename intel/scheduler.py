"""投递时段与频率。

两种模式，用户二选一：
  times    —— 每天固定几个时点（早报/晚报）。最可预期，推荐默认。
  interval —— 在活跃时段内每 N 分钟跑一次。适合「有事就推」的强时效场景。

两者共用同一套约束：
  weekdays      —— 只在指定星期运行（ISO：1=周一 … 7=周日）
  active_hours  —— 活跃时段，interval 模式在此窗口内取格子；times 模式下
                   落在窗口外的时点会被跳过（防止配了 03:00 却抱怨半夜推送）
  timezone      —— 一律按用户本地时区算，服务器时区不参与
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .util import now_local

MIN_INTERVAL_MINUTES = 5


def parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hour, minute = (int(x) for x in str(value).strip().split(":", 1))
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _window(cfg: dict[str, Any]) -> tuple[tuple[int, int], tuple[int, int]] | None:
    hours = cfg.get("active_hours") or []
    if len(hours) != 2:
        return None
    start, end = parse_hhmm(hours[0]), parse_hhmm(hours[1])
    if not start or not end:
        return None
    return start, end


def _weekdays(cfg: dict[str, Any]) -> set[int]:
    raw = cfg.get("weekdays")
    days: set[int] = set()
    for value in raw or []:
        try:
            day = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 7:
            days.add(day)
    return days or {1, 2, 3, 4, 5, 6, 7}


def _allowed(day: datetime, weekdays: set[int]) -> bool:
    return day.isoweekday() in weekdays


def _at(day: datetime, hhmm: tuple[int, int]) -> datetime:
    return day.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)


def _in_window(moment: datetime, window: tuple[tuple[int, int], tuple[int, int]] | None) -> bool:
    if not window:
        return True
    start, end = window
    start_dt = _at(moment, start)
    end_dt = _at(moment, end)
    if end_dt <= start_dt:  # 跨午夜
        return moment >= start_dt or moment <= end_dt
    return start_dt <= moment <= end_dt


def next_run_at(cfg: dict[str, Any], after: datetime | None = None) -> datetime:
    """返回下一次应当运行的时间（带时区）。永远向前找，不会返回过去。"""
    tz_name = cfg.get("timezone") or "Asia/Shanghai"
    now = after or now_local(tz_name)
    if now.tzinfo is None:
        now = now.replace(tzinfo=now_local(tz_name).tzinfo)

    mode = str(cfg.get("mode") or "times").lower()
    weekdays = _weekdays(cfg)
    window = _window(cfg)

    if mode == "interval":
        minutes = max(MIN_INTERVAL_MINUTES, int(cfg.get("interval_minutes") or 120))
        for offset in range(0, 9):
            day = now + timedelta(days=offset)
            if not _allowed(day, weekdays):
                continue
            if window:
                start_dt = _at(day, window[0])
                end_dt = _at(day, window[1])
                if end_dt <= start_dt:
                    end_dt = end_dt + timedelta(days=1)
            else:
                start_dt = _at(day, (0, 0))
                end_dt = _at(day, (23, 59))
            slot = start_dt
            while slot <= end_dt:
                if slot > now:
                    return slot
                slot += timedelta(minutes=minutes)
        return now + timedelta(hours=1)

    # times 模式
    times = [parse_hhmm(v) for v in (cfg.get("times") or ["08:30"])]
    times = sorted(t for t in times if t)
    if not times:
        times = [(8, 30)]
    for offset in range(0, 9):
        day = now + timedelta(days=offset)
        if not _allowed(day, weekdays):
            continue
        for hhmm in times:
            slot = _at(day, hhmm)
            if slot <= now:
                continue
            if window and not _in_window(slot, window):
                continue
            return slot
    return now + timedelta(hours=24)


def describe(cfg: dict[str, Any]) -> str:
    """给 CLI / doctor 用的一句话说明。"""
    mode = str(cfg.get("mode") or "times").lower()
    weekdays = _weekdays(cfg)
    day_names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
    if weekdays == {1, 2, 3, 4, 5, 6, 7}:
        days = "每天"
    else:
        days = "每周" + "、".join(day_names[d] for d in sorted(weekdays))
    window = cfg.get("active_hours") or []
    win = f"，活跃 {window[0]}–{window[1]}" if len(window) == 2 else ""
    if mode == "interval":
        return f"{days}每 {int(cfg.get('interval_minutes') or 120)} 分钟{win}（{cfg.get('timezone') or 'Asia/Shanghai'}）"
    return f"{days} {'、'.join(cfg.get('times') or ['08:30'])}{win}（{cfg.get('timezone') or 'Asia/Shanghai'}）"


def _briefing_schedule(cfg: dict[str, Any]) -> dict[str, Any] | None:
    schedule = cfg.get("schedule") or {}
    brief = schedule.get("briefing") or {}
    if not brief.get("enabled"):
        return None
    return {
        "mode": "times",
        "times": [str(brief.get("time") or "21:30")],
        "weekdays": schedule.get("weekdays") or [1, 2, 3, 4, 5, 6, 7],
        "timezone": schedule.get("timezone") or "Asia/Shanghai",
        "active_hours": [],  # 简报是夜间兜底，不受活跃时段限制
    }


def next_event(cfg: dict[str, Any], after: datetime | None = None) -> tuple[datetime, str, str]:
    """多关注项 + 简报的统一下一次触发点。

    返回 (时间, kind, topic_id)，kind 为 run / brief。
    每个关注项按自己的频率算，谁先到就先跑；简报独立于关注项。
    """
    from .topics import list_topics

    candidates: list[tuple[datetime, str, str]] = []
    for topic in list_topics(cfg):
        if not topic.get("enabled", True):
            continue
        candidates.append((next_run_at(topic.get("schedule") or {}, after=after),
                           "run", topic["id"]))
    brief = _briefing_schedule(cfg)
    if brief:
        candidates.append((next_run_at(brief, after=after), "brief", ""))
    if not candidates:
        return (after or now_local(cfg["schedule"].get("timezone", "Asia/Shanghai"))
                ) + timedelta(hours=1), "run", ""
    return min(candidates, key=lambda row: row[0])


def describe_all(cfg: dict[str, Any]) -> str:
    """多关注项时的整体说明：每个关注项一行 + 简报时间。"""
    from .topics import list_topics

    parts = [f"{tp['id'] or 'default'}：{describe(tp.get('schedule') or {})}"
             for tp in list_topics(cfg) if tp.get("enabled", True)]
    brief = _briefing_schedule(cfg)
    if brief:
        parts.append(f"简报：每天 {brief['times'][0]}")
    return "；".join(parts)
