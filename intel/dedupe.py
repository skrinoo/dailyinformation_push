"""L1 去重：URL + 近似标题。

参数照抄 Inoreader 公开的选项（80% 共同词、30% 长度差、短标题词数必须相等、
默认 24h 窗口、保留最早一篇），并在其上补两条工程约束：

1. 用倒排索引先筛候选，避免 O(n²) 全量比对；
2. 每次折叠都写一条 audit 记录，没有审计的去重用户不敢开。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .models import Item
from .normalize import published_dt
from .util import jaccard, tokenize


def _similarity(a: set[str], b: set[str]) -> float:
    """共同词占比（containment），不是 Jaccard。

    转载稿通常会在原标题上「加料」（副标题、括号补充），长度差在 30% 以内时
    Jaccard 会被新增词稀释到 0.6 左右，永远够不到 0.80。Inoreader 说的
    「共同词占比 80%」对应的正是 containment：交集 / 较短标题词数。
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(min(len(a), len(b)))


def _too_different_in_length(a: set[str], b: set[str], ratio_max: float) -> bool:
    if not a or not b:
        return True
    shorter, longer = sorted((len(a), len(b)))
    return (longer - shorter) / float(longer) > ratio_max


def _is_dup(a: Item, a_tokens: set[str], b: Item, b_tokens: set[str], cfg: dict[str, Any]) -> bool:
    if a.canonical_url and a.canonical_url == b.canonical_url:
        return True

    short_words = int(cfg.get("short_title_words", 5))
    if len(a_tokens) <= short_words or len(b_tokens) <= short_words:
        # 短标题只允许词数完全相同 + 高相似，防止 "OpenAI 发布" 误杀 "OpenAI 融资"
        if len(a_tokens) != len(b_tokens):
            return False

    if _too_different_in_length(a_tokens, b_tokens, float(cfg.get("length_ratio_max", 0.30))):
        return False

    return _similarity(a_tokens, b_tokens) >= float(cfg.get("title_threshold", 0.80))


def dedupe(items: list[Item], cfg: dict[str, Any]) -> tuple[list[Item], list[dict[str, Any]]]:
    """返回 (保留条目, 折叠审计记录)。保留策略：同簇内取最早发布。"""
    window = timedelta(hours=float(cfg.get("window_hours", 24)))
    ordered = sorted(items, key=lambda it: it.published_at or "")
    kept: list[Item] = []
    kept_tokens: list[set[str]] = []
    index: dict[str, list[int]] = {}
    audit: list[dict[str, Any]] = []

    for item in ordered:
        tokens = tokenize(item.title)
        candidates: set[int] = set()
        for tok in tokens:
            candidates.update(index.get(tok, []))

        dup_of: int | None = None
        for pos in candidates:
            prev = kept[pos]
            if abs((published_dt(item) - published_dt(prev)).total_seconds()) > window.total_seconds():
                continue
            if _is_dup(item, tokens, prev, kept_tokens[pos], cfg):
                dup_of = pos
                break

        if dup_of is not None:
            # 合并来源信息，便于展示"另有 N 家报道"
            target = kept[dup_of]
            for src in item.sources or [item.source]:
                if src and src not in target.sources:
                    target.sources.append(src)
            target.meta.setdefault("folded", []).append(
                {"id": item.id, "source": item.source, "title": item.title}
            )
            audit.append({
                "dropped_id": item.id,
                "dropped_title": item.title,
                "duplicate_of": target.id,
                "kept_title": target.title,
                "reason": "same_url" if item.canonical_url == target.canonical_url else "similar_title",
            })
            continue

        pos = len(kept)
        kept.append(item)
        kept_tokens.append(tokens)
        for tok in tokens:
            index.setdefault(tok, []).append(pos)

    return kept, audit
