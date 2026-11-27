"""粗排：不调用任何模型，把候选量压到 1/5 ~ 1/10。

这一步存在的唯一理由是成本。300 条候选全过模型要 300 次调用，
粗排之后只剩 30-40 条，模型成本降一个数量级。

但粗排必须「只降分、不淘汰」（屏蔽词除外）——启发式误杀一条好内容，
后面没有任何环节能把它救回来。淘汰权只交给四道闸。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .models import Item
from .normalize import published_dt
from .util import freshness


def _keyword_score(item: Item, cfg: dict[str, Any]) -> float:
    keywords = [k.lower() for k in cfg.get("keywords", []) if k]
    if not keywords:
        return 0.0
    title = (item.title or "").lower()
    body = (item.raw_summary or "").lower()
    title_hits = sum(1 for k in keywords if k in title)
    body_hits = sum(1 for k in keywords if k in body)
    return min(4.0, title_hits * 1.1 + body_hits * 0.4)


def _topic_score(item: Item, cfg: dict[str, Any]) -> float:
    """GitHub 条目的主题门槛：topics/language 都不匹配时说明是搜索副产物。"""
    if item.source != "github":
        return 0.0
    gh = cfg.get("_github_cfg") or {}
    topics = {t.lower() for t in (gh.get("topics") or [])}
    languages = {l.lower() for l in (gh.get("languages") or [])}
    item_topics = {str(t).lower() for t in (item.meta.get("topics") or [])}
    lang = str(item.meta.get("language") or "").lower()

    matched = bool(topics & item_topics) or (lang and lang in languages)
    return 0.8 if matched else -1.8


def _heat_score(item: Item) -> float:
    """热度用增速而非总量：否则日报会连推一个月同一个常青项目。"""
    if item.source != "github":
        return 0.0
    stars = float(item.meta.get("stars") or 0)
    velocity = float(item.meta.get("velocity") or 0)
    if velocity <= 0 and stars > 0:
        created = item.meta.get("created_at") or ""
        try:
            age_days = max(
                1.0,
                (datetime.now(timezone.utc) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days,
            )
        except Exception:
            age_days = 365.0
        velocity = stars / age_days
    base = min(2.0, math.log10(stars + 1) * 0.5)
    return base + min(2.0, math.log10(velocity + 1) * 1.1)


def _blocked(item: Item, cfg: dict[str, Any]) -> str:
    text = f"{item.title} {item.raw_summary}".lower()
    for word in cfg.get("block_keywords", []):
        if word and word.lower() in text:
            return word
    return ""


def score_item(item: Item, cfg: dict[str, Any], source_weight: float = 1.0) -> float:
    score = 2.0
    score += _keyword_score(item, cfg)
    score += _topic_score(item, cfg)
    score += _heat_score(item)
    score += 2.0 * freshness(max(0.0, (datetime.now(timezone.utc) - published_dt(item)).total_seconds() / 3600.0))
    score += max(-1.0, min(1.5, (source_weight - 1.0) * 1.5))
    return max(0.0, min(10.0, score))


def coarse_rank(
    items: list[Item], cfg: dict[str, Any], weights: dict[str, float]
) -> tuple[list[Item], list[dict[str, Any]]]:
    """返回 (按粗排分降序的候选, 屏蔽记录)。屏蔽是唯一允许的硬淘汰。"""
    filt = cfg["filter"]
    cfg["_github_cfg"] = cfg["sources"].get("github", {})

    blocked: list[dict[str, Any]] = []
    alive: list[Item] = []
    for item in items:
        hit = _blocked(item, filt)
        if hit:
            blocked.append({"id": item.id, "title": item.title, "reason": f"blocked:{hit}"})
            item.verdict = "rejected"
            item.reject_reason = f"blocked_keyword:{hit}"
            continue
        item.meta["coarse"] = round(score_item(item, filt, weights.get(item.source, 1.0)), 2)
        alive.append(item)

    alive.sort(key=lambda it: it.meta["coarse"], reverse=True)
    keep = max(
        int(filt.get("coarse_min_keep", 12)),
        int(len(alive) * float(filt.get("coarse_keep_ratio", 0.28))),
    )
    keep = min(keep, int(filt.get("max_candidates_for_llm", 40)))
    selected = list(alive[:keep])

    # 来源保底：粗排是召回层，不能让某个源在进入判定前就被整体清零。
    # 中文源的绝对分普遍低于 GitHub（没有 star 增速可加），如果只看全局排序，
    # 它们会稳定地在粗排被清空——然后「中英文都要」就只剩英文了。
    if filt.get("coarse_source_floor", True):
        counts: dict[str, int] = {}
        for it in selected:
            counts[it.source] = counts.get(it.source, 0) + 1
        for source in {it.source for it in alive}:
            if counts.get(source):
                continue
            best = next(it for it in alive if it.source == source)
            if len(selected) < keep:
                selected.append(best)
                counts[source] = 1
                continue
            # 没有空位：换掉分数最低、且来源还有第二条的尾部条目
            victims = [it for it in selected if counts.get(it.source, 0) >= 2]
            if not victims:
                continue
            victim = min(victims, key=lambda it: it.meta["coarse"])
            selected.remove(victim)
            counts[victim.source] -= 1
            selected.append(best)
            counts[source] = 1

    selected.sort(key=lambda it: it.meta["coarse"], reverse=True)
    return selected, blocked
