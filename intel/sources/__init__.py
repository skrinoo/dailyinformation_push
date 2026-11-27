"""采集编排：单源失败隔离 + AI 检索策略落点。

任何一个源挂掉（GitHub 403、RSS 502、页面超时），只跳过该源并记一条告警，
不能让「今天没有简报」。这是 Google Alerts 式静默失效的反面。

plan 参数来自 aiplan.get_plan()：结构化查询列表。没有 plan 时各源回落到
配置里的确定性查询，所以模型不可用也不会让采集停摆。
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..models import Item
from . import github, hn, page, rss


def _github_token(cfg: dict[str, Any]) -> str:
    env_name = cfg["sources"]["github"].get("token_env") or "GITHUB_TOKEN"
    return os.environ.get(env_name, "").strip()


def fetch_all(
    cfg: dict[str, Any],
    store: Any = None,
    tracer: Any = None,
    offline_items: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
) -> tuple[list[Item], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    items: list[Item] = []
    plan_queries = (plan or {}).get("queries") or []

    if offline_items is not None:
        from ..normalize import normalize_item
        items = [normalize_item(raw) for raw in offline_items]
        if tracer:
            tracer.event("fetch.offline", count=len(items))
        return items, errors

    deadline = float(cfg.get("runtime", {}).get("source_deadline_seconds", 90))

    # ---- GitHub ----
    if cfg["sources"].get("github", {}).get("enabled"):
        started = time.perf_counter()
        try:
            if tracer:
                with tracer.span("fetch.github") as payload:
                    got = github.fetch(cfg, _github_token(cfg), plan_queries)
                    payload["count"] = len(got)
            else:
                got = github.fetch(cfg, _github_token(cfg), plan_queries)
            items.extend(got)
        except Exception as exc:
            errors.append({"source": "github", "error": str(exc)[:300]})
            if tracer:
                tracer.event("fetch.error", source="github", error=str(exc)[:200])
        finally:
            if time.perf_counter() - started > deadline:
                errors.append({"source": "github", "error": "超过单源时限，结果可能不完整"})

    # ---- HN Algolia（AI 规划查询的主要落点）----
    if (cfg["sources"].get("hn") or {}).get("enabled"):
        try:
            if tracer:
                with tracer.span("fetch.hn") as payload:
                    got = hn.fetch(cfg, plan_queries)
                    payload["count"] = len(got)
            else:
                got = hn.fetch(cfg, plan_queries)
            items.extend(got)
        except Exception as exc:
            errors.append({"source": "hn", "error": str(exc)[:300]})
            if tracer:
                tracer.event("fetch.error", source="hn", error=str(exc)[:200])

    # ---- RSS（中英文并列）----
    for feed in cfg["sources"].get("rss", []):
        name = feed.get("name") or feed.get("url") or "rss"
        if feed.get("enabled") is False:
            continue
        try:
            if tracer:
                with tracer.span("fetch.rss", feed=name) as payload:
                    got = rss.fetch_one(feed)
                    payload["count"] = len(got)
            else:
                got = rss.fetch_one(feed)
            items.extend(got)
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)[:300]})
            if tracer:
                tracer.event("fetch.error", source=name, error=str(exc)[:200])

    # ---- Page Monitor ----
    page_cfg = cfg["sources"].get("page", {})
    if page_cfg.get("enabled") and store is not None:
        try:
            items.extend(page.fetch(page_cfg, store))
        except Exception as exc:
            errors.append({"source": "page", "error": str(exc)[:300]})

    return items, errors
