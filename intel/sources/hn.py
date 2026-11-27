"""Hacker News 检索源（Algolia API）。

与 hnrss 的区别：hnrss 只能拿「首页/最新」，拿不到按关键词的历史检索；
Algolia 可以按 query + points 阈值取，这正是 AI 规划出来的查询要落到的地方。

接口无需鉴权，限流比 GitHub 宽松，但仍按单查询隔离：一条查询失败不影响其它查询。
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import timedelta
from typing import Any

from ..models import Item
from ..util import clip, http_get_text, parse_datetime, sha1, strip_html, utcnow

API = "https://hn.algolia.com/api/v1/search_by_date"


def _item_from_hit(hit: dict[str, Any], query: str) -> Item | None:
    title = (hit.get("title") or hit.get("story_title") or "").strip()
    if not title:
        return None
    hn_id = str(hit.get("objectID") or "")
    url = (hit.get("url") or "").strip()
    if not url and hn_id:
        url = f"https://news.ycombinator.com/item?id={hn_id}"
    if not url:
        return None

    created = parse_datetime(hit.get("created_at"))
    published = created.isoformat(timespec="seconds") if created else ""
    summary = strip_html(hit.get("story_text") or hit.get("comment_text") or "")
    points = int(hit.get("points") or 0)
    comments = int(hit.get("num_comments") or 0)

    return Item(
        id=sha1(url)[:16],
        title=clip(title, 200),
        url=url,
        canonical_url="",
        source="Hacker News",
        published_at=published,
        raw_summary=clip(summary, 600),
        meta={
            "points": points,
            "comments": comments,
            "hn_id": hn_id,
            "hn_query": query,
            "lang": "en",
        },
    )


def fetch_query(query: str, min_points: int = 20, limit: int = 15,
                since_hours: int = 48) -> list[Item]:
    since_ts = int((utcnow() - timedelta(hours=since_hours)).timestamp())
    params = {
        "query": query,
        "tags": "story",
        "hitsPerPage": str(max(5, min(50, limit))),
        "numericFilters": f"points>{max(0, min_points - 1)},created_at_i>{since_ts}",
    }
    url = f"{API}?{urllib.parse.urlencode(params)}"
    payload = json.loads(http_get_text(url, timeout=25))
    out: list[Item] = []
    for hit in payload.get("hits") or []:
        item = _item_from_hit(hit, query)
        if item:
            out.append(item)
    return out


def fetch(cfg: dict[str, Any], plan_queries: list[dict[str, Any]] | None = None) -> list[Item]:
    hn = cfg["sources"].get("hn") or {}
    if not hn.get("enabled"):
        return []

    limit = int(hn.get("limit") or 15)
    since_hours = int(hn.get("since_hours") or 48)
    default_points = int(hn.get("min_points") or 20)

    queries: list[dict[str, Any]] = [q for q in (plan_queries or []) if q.get("kind") == "hn"]
    if not queries:
        queries = [{"query": q, "min_points": default_points}
                   for q in (hn.get("queries") or [])]

    items: dict[str, Item] = {}
    errors: list[str] = []
    for row in queries[:6]:
        query = str(row.get("query") or "").strip()
        if not query:
            continue
        try:
            got = fetch_query(query, int(row.get("min_points") or default_points),
                              limit=limit, since_hours=since_hours)
            for item in got:
                items[item.id] = item
        except Exception as exc:  # 单查询失败不拖垮整源
            errors.append(f"{query}: {str(exc)[:120]}")
    if errors and not items:
        raise RuntimeError("; ".join(errors[:2]))
    return list(items.values())
