"""配额、截断与分流落位。

两阶段，而不是一次遍历：

  阶段一（严格）—— 单源上限 floor(cap × 60%)。先保证多样性，
                   免得 GitHub 靠分数高吃满全部名额。
  阶段二（补位）—— 严格阶段没填满时，按分数把溢出条目补回来。
                   否则「其他源今天没内容」会让日报从 6 条缩成 3 条，
                   这不是宁缺毋滥，是白白浪费名额。

被截断的条目 verdict 仍是 selected（内容合格），只是 channel 降为 archive，
方便用户回查「今天还有哪些入围但没推」。
"""

from __future__ import annotations

import math
from typing import Any

from .models import CHANNEL_ARCHIVE, Item


def _lang(item: Item) -> str:
    return str(item.meta.get("lang") or "en").lower()


def _apply_language_mix(picked: list[Item], passed: list[Item],
                        cap: int, cfg: dict[str, Any]) -> list[str]:
    """中英文配比。

    只在「已通过四闸」的候选之间换位，绝不用不达标的内容凑比例。
    换位还要求分数不塌陷（容忍 1.0 分以内），否则宁可不换——
    为了凑语言比例把一条 5.5 分的换成 3.1 分的，是本末倒置。
    """
    mix = cfg["filter"].get("language_mix") or {}
    if not mix.get("enabled"):
        return []
    ratio = max(0.0, min(0.8, float(mix.get("zh_ratio", 0.34))))
    zh_in_passed = [it for it in passed if _lang(it) == "zh"]
    if not zh_in_passed:
        return []

    target_zh = max(1, round(cap * ratio))
    notes: list[str] = []
    picked_ids = {it.id for it in picked}

    for cand in zh_in_passed:
        if sum(1 for it in picked if _lang(it) == "zh") >= target_zh:
            break
        if cand.id in picked_ids:
            continue
        en_items = [it for it in picked if _lang(it) == "en"]
        if len(en_items) <= 1:  # 不能把英文换空
            break
        victim = min(en_items, key=lambda it: it.score)
        if victim.score - cand.score > 1.0:  # 质量塌陷过大就不换
            continue
        picked.remove(victim)
        victim.channel = CHANNEL_ARCHIVE
        victim.meta["overflow_reason"] = "language_mix"
        picked.append(cand)
        picked_ids.add(cand.id)
        notes.append(f"{victim.source}→{cand.source}")
    return notes


def apply_quota(items: list[Item], cfg: dict[str, Any]) -> dict[str, Any]:
    cap = int(cfg["filter"].get("max_items_per_run", 8))
    ratio = float(cfg["filter"].get("source_quota_ratio", 0.6))
    per_source = max(1, math.floor(cap * ratio))

    passed = [it for it in items if it.passed]
    passed.sort(key=lambda it: (-it.score, -it.confidence, it.published_at or ""))

    picked: list[Item] = []
    counts: dict[str, int] = {}
    overflow: list[tuple[Item, str]] = []

    # 阶段一：严格配额
    for item in passed:
        if len(picked) >= cap:
            overflow.append((item, "cap_reached"))
            continue
        src = item.source or "unknown"
        if counts.get(src, 0) >= per_source:
            overflow.append((item, f"source_quota:{src}"))
            continue
        picked.append(item)
        counts[src] = counts.get(src, 0) + 1

    # 阶段二：补位（只用被「单源配额」挡下的，capped 的不再补）
    # 补位可以突破 60%，但不能让单一来源吃满全部——那是 80% 的硬顶。
    top_source_cap = max(per_source + 1, math.ceil(cap * 0.8))
    shortfall = cap - len(picked)
    if shortfall > 0:
        for item, reason in overflow:
            if shortfall <= 0:
                break
            if not reason.startswith("source_quota"):
                continue
            src = item.source or "unknown"
            if counts.get(src, 0) >= top_source_cap:
                continue
            picked.append(item)
            counts[src] = counts.get(src, 0) + 1
            shortfall -= 1

    # 阶段三：语言配比（只换位，不降标准）
    mix_notes = _apply_language_mix(picked, passed, cap, cfg)

    counts = {}
    for item in picked:
        src = item.source or "unknown"
        counts[src] = counts.get(src, 0) + 1

    picked_ids = {it.id for it in picked}
    for item, reason in overflow:
        if item.id in picked_ids:
            item.channel = item.channel or CHANNEL_ARCHIVE
            continue
        item.channel = CHANNEL_ARCHIVE
        item.meta["overflow_reason"] = reason

    picked.sort(key=lambda it: (-it.score, -it.confidence))

    by_lang: dict[str, int] = {}
    for item in picked:
        key = _lang(item)
        by_lang[key] = by_lang.get(key, 0) + 1

    return {
        "picked": picked,
        "candidates": len(passed),
        "overflow": [
            {"id": it.id, "title": it.title, "reason": reason}
            for it, reason in overflow if it.id not in picked_ids
        ],
        "per_source_cap": per_source,
        "by_source": counts,
        "by_language": by_lang,
        "language_mix": mix_notes,
    }
