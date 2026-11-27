"""归一化与实体抽取。

实体是 L2 事件簇的主键，所以抽取策略必须「宁可漏合，不可误合」——
把两条无关新闻合成一个事件，用户会立刻察觉系统很蠢；漏合只是多出一条。

因此只对两种高置信实体做跨源合并：
  gh:owner/repo   —— GitHub 仓库，天然唯一
  kw:<专有名词>    —— 标题里的大写英文专名（OpenAI / Anthropic / llama.cpp）
域名类实体（dom:）只用于同源内聚类，不参与跨源合并。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .models import Item
from .util import canonical_url, clip, parse_datetime, sha1, strip_html, utcnow

# 标题里的功能词，不能当实体（否则 "Show HN: ..." 会全部聚到一起）
STOPWORDS = {
    "the", "a", "an", "new", "show", "hn", "how", "why", "what", "when", "who",
    "this", "that", "these", "those", "and", "or", "for", "with", "from", "into",
    "your", "you", "our", "we", "they", "it", "is", "are", "was", "were", "be",
    "i", "my", "me", "on", "in", "of", "to", "at", "by", "as", "vs", "via",
    "introducing", "announcing", "release", "released", "update", "updated",
    "weekly", "daily", "edition", "issue", "part", "episode",
    "ai", "llm", "api", "app", "tool", "tools",
}

_PROPER = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*\b")
_CJK_BRACKET = re.compile(r"[《「“\"]([^》」”\"]{2,24})[》」”\"]")
_GH_URL = re.compile(r"github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)")


def _github_repo(url: str) -> str:
    m = _GH_URL.search(url or "")
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in {"topics", "search", "trending", "collections", "features", "about"}:
        return ""
    return f"{owner}/{repo}".lower()


def _proper_nouns(title: str) -> list[str]:
    """按「出现位置」返回候选专名，不是按长度。

    标题的第一个专名通常是主语（OpenWrt / Anthropic / OpenAI），
    按长度排序会把 "WireGuard" 这种从句里的技术名当成主体，
    导致同一事件的第二篇报道抽到不同实体，永远聚不到一起。
    """
    seen: list[str] = []
    for raw in _PROPER.findall(title or ""):
        token = raw.strip(".-")
        low = token.lower()
        if len(token) < 3 or low in STOPWORDS or token.isdigit():
            continue
        if token.isupper() and len(token) <= 2:  # HN / AI 这类缩写
            continue
        if low not in [s.lower() for s in seen]:
            seen.append(token)
    return seen


def extract_entities(item: Item) -> list[str]:
    """返回实体 key 列表，第一个是主实体。空列表表示「不适合跨源合并」。"""
    repo = _github_repo(item.url)
    if repo:
        return [f"gh:{repo}"]

    ents: list[str] = []
    for name in _proper_nouns(item.title)[:2]:
        ents.append(f"kw:{name.lower()}")

    for m in _CJK_BRACKET.findall(item.title or ""):
        ents.append(f"kw:{m.strip()}")

    if not ents:
        # 没有专名时退回域名 + 路径首段：稳定但不跨源
        from urllib.parse import urlsplit

        parts = urlsplit(item.url)
        host = (parts.hostname or "").replace("www.", "")
        seg = parts.path.strip("/").split("/")[0] if parts.path.strip("/") else ""
        if host:
            ents.append(f"dom:{host}/{seg}"[:80])
    # 去重保序
    out: list[str] = []
    for e in ents:
        if e and e not in out:
            out.append(e)
    return out[:2]


def normalize_item(raw: dict[str, Any]) -> Item:
    url = (raw.get("url") or "").strip()
    canon = canonical_url(url)
    title = clip(re.sub(r"\s+", " ", strip_html(raw.get("title") or "")).strip(), 300)
    summary = clip(re.sub(r"\s+", " ", strip_html(raw.get("raw_summary") or "")).strip(), 1200)

    published = raw.get("published_at") or ""
    meta = dict(raw.get("meta") or {})
    dt = parse_datetime(published)
    if dt is None:
        published = utcnow().isoformat(timespec="seconds")
        meta["time_estimated"] = True
    else:
        published = dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    item = Item(
        id=sha1(canon or title)[:16],
        title=title,
        url=url,
        canonical_url=canon,
        source=raw.get("source") or "",
        published_at=published,
        raw_summary=summary,
        tags=list(raw.get("tags") or []),
        sources=list(raw.get("sources") or [raw.get("source") or ""]),
        meta=meta,
    )
    item.entities = extract_entities(item)
    return item


def published_dt(item: Item) -> datetime:
    return parse_datetime(item.published_at) or datetime.now(timezone.utc)
