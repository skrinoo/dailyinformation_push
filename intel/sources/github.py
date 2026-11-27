from __future__ import annotations

import json
import urllib.parse
from datetime import timedelta
from typing import Any

from ..models import Item
from ..util import clip, http_get_text, parse_datetime, sha1, strip_html, utcnow

API = "https://api.github.com/search/repositories"


def _query(topic: str | None, language: str | None, since: str, min_stars: int,
           new_only: bool = False, keywords: str = "") -> str:
    parts = []
    if topic:
        parts.append(f"topic:{topic}")
    if keywords:
        parts.append(f"{keywords} in:name,description,readme")
    if language:
        parts.append(f'language:"{language}"')
    parts.append(f"stars:>={min_stars}")
    parts.append(f"created:>={since}" if new_only else f"pushed:>={since}")
    return " ".join(parts)


def _queries_from_plan(plan_queries: list[dict[str, Any]], since: str,
                       min_stars: int) -> list[tuple[str, str]]:
    """把 AI 规划的结构化查询拼成 GitHub 语法，附带 why 用于溯源。

    只接受白名单字段（topic / language / keywords），不把模型给的整串 query
    直接塞进 API——否则一次提示注入就能让检索跑偏。
    """
    out: list[tuple[str, str]] = []
    for row in plan_queries or []:
        if row.get("kind") != "github":
            continue
        q = _query(
            row.get("topic") or None,
            row.get("language") or None,
            since, min_stars,
            new_only=bool(row.get("new_only")),
            keywords=str(row.get("keywords") or ""),
        )
        if q and (q, row.get("why") or "") not in out:
            out.append((q, str(row.get("why") or "")))
    return out


def fetch(cfg: dict[str, Any], token: str = "",
          plan_queries: list[dict[str, Any]] | None = None) -> list[Item]:
    gh = cfg["sources"]["github"]
    if not gh.get("enabled"):
        return []

    days = int(gh.get("since_days", 14))
    since = (utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    min_stars = int(gh.get("min_stars", 50))
    per_query = max(10, int(gh.get("limit", 20)))
    topics = list(gh.get("topics") or [])
    languages = list(gh.get("languages") or [])

    planned = _queries_from_plan(plan_queries or [], since, min_stars)
    if planned:
        queries = planned
    else:
        queries = []
        for topic in topics[:4]:
            queries.append((_query(topic, languages[0] if len(languages) == 1 else None,
                                   since, min_stars), "配置中的主题"))
        for language in languages[:2]:
            queries.append((_query(None, language, since, min_stars, new_only=True), "配置中的语言新库"))
        if topics:
            queries.append((_query(topics[0], None, since, min_stars, new_only=True), "主题新库"))

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    items: dict[str, Item] = {}
    errors: list[str] = []
    for q, why in queries[:8]:
        url = f"{API}?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page={per_query}"
        try:
            payload = json.loads(http_get_text(url, headers=headers, timeout=30))
        except Exception as exc:  # one failed query must not drop the whole source
            errors.append(str(exc))
            continue
        for repo in payload.get("items", []):
            html_url = repo.get("html_url", "")
            if not html_url:
                continue
            topics_list = repo.get("topics") or []
            created = parse_datetime(repo.get("created_at"))
            is_new = bool(created and (utcnow() - created).days <= days)
            stars = repo.get("stargazers_count", 0) or 0
            age_days = max(1.0, (utcnow() - created).total_seconds() / 86400) if created else 365.0
            desc = repo.get("description") or ""
            item = Item(
                id=sha1(html_url)[:16],
                title=repo.get("full_name", "") or repo.get("name", ""),
                url=html_url,
                source="github",
                published_at=repo.get("pushed_at", ""),
                raw_summary=strip_html(desc),
                meta={
                    "stars": stars,
                    "forks": repo.get("forks_count", 0),
                    "language": repo.get("language") or "",
                    "topics": topics_list[:8],
                    "is_new": is_new,
                    "created_at": repo.get("created_at", ""),
                    "velocity": round(stars / age_days, 1),
                    "query_why": why,
                    "lang": "en",
                },
            )
            items[item.id] = item
    if not items and errors:
        hint = " (set GITHUB_TOKEN to raise the rate limit)" if any("403" in e for e in errors) else ""
        raise RuntimeError(errors[0][:200] + hint)
    return list(items.values())


def describe(item: Item) -> str:
    stars = item.meta.get("stars", 0)
    lang = item.meta.get("language", "")
    topic = " / ".join(item.meta.get("topics", [])[:3])
    bits = [f"★{stars}", lang, topic]
    return clip(" · ".join(b for b in bits if b), 90)
