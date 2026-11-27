"""水位线：只处理「上次之后」的新条目。

三重防线，缺一不可：
  1. 时间水位：published_at > last_seen - overlap（overlap 兜住发布延迟与时钟漂移）
  2. URL 已知：该关注项见过的 canonical_url 直接跳过（防止同一文章换参数重发）
  3. 推送幂等：30 天内推过的 URL 不再推（watermark 失效时的最后一道）

多关注项时，水位线与「已知 URL」都按 topic 分区：
关注项 A 把某个源推进到 10:00，不能让关注项 B 漏掉 09:00 的条目；
A 认为无关而丢掉的条目，B 仍然有权收一次。

被淘汰的条目也会入库，所以「阈值调低后重评历史」不会自动发生。
这是刻意的：重评会重复烧模型预算。需要重评时用 `--reset-watermark`。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import Item
from .util import parse_datetime


def _wm_key(source: str, topic: str) -> str:
    """水位线键名。单关注项保持原键名，老库不用迁移。"""
    return f"{topic}|{source}" if topic else source


def filter_new(
    items: list[Item], store: Any, cfg: dict[str, Any], ignore_history: bool = False,
    topic: str = "",
) -> tuple[list[Item], list[dict[str, Any]]]:
    """ignore_history=True 时只做「时间窗」过滤，用于 --reset-watermark 重评历史。

    注意：重评会重新烧 token，所以默认不这么做；仅清空 watermark 表是不够的，
    因为「URL 已知」是第二道防线，库存里还有记录。
    """
    wm_cfg = cfg.get("watermark", {})
    overlap = timedelta(hours=float(wm_cfg.get("overlap_hours", 6)))
    max_backfill = float(wm_cfg.get("max_backfill_hours", 168))
    enabled = bool(wm_cfg.get("enabled", True))
    now = datetime.now(timezone.utc)

    by_source: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        by_source[item.source or "unknown"].append(item)

    kept: list[Item] = []
    audit: list[dict[str, Any]] = []

    for source, group in by_source.items():
        key = _wm_key(source, topic)
        wm = store.get_watermark(key) if (enabled and not ignore_history) else {"last_seen_at": ""}
        last_seen = parse_datetime(wm.get("last_seen_at") or "")
        cutoff = (last_seen - overlap) if last_seen else (now - timedelta(hours=max_backfill))

        for item in group:
            published = parse_datetime(item.published_at)
            if published and published < cutoff:
                audit.append({"id": item.id, "source": source, "reason": "before_watermark",
                              "published_at": item.published_at})
                continue
            if not ignore_history and item.canonical_url and store.knows_url(item.canonical_url, topic):
                audit.append({"id": item.id, "source": source, "reason": "known_url"})
                continue
            if (not ignore_history and item.canonical_url
                    and store.pushed_within(item.canonical_url, 30 * 24)):
                audit.append({"id": item.id, "source": source, "reason": "pushed_within_30d"})
                continue
            kept.append(item)

    return kept, audit


def advance(items: list[Item], store: Any, topic: str = "") -> None:
    """把每个源的水位线推进到本批最大的 published_at。"""
    latest: dict[str, str] = {}
    for item in items:
        src = item.source or "unknown"
        if item.published_at and item.published_at > latest.get(src, ""):
            latest[src] = item.published_at
    for src, ts in latest.items():
        store.set_watermark(_wm_key(src, topic), ts)
