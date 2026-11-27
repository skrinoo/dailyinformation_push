"""L2 事件簇：把「同一件事的多家报道」折成一个事件。

合并条件同时满足三条，缺一不可：
  1. 主实体 key 相同（gh:owner/repo 或 kw:专名）
  2. 发布时间差 <= window_hours（默认 72h）
  3. 标题相似度 >= min_title_similarity（默认 0.25）

第三条是防误合的关键：同一个实体在三天内可能发两个不同产品，
只靠实体 + 时间会把它们合并成一条，用户看到会直接判定系统坏了。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .models import Cluster, Item
from .normalize import published_dt
from .util import jaccard, sha1, tokenize


def _merge_key(item: Item) -> str:
    for ent in item.entities:
        if ent.startswith("gh:") or ent.startswith("kw:"):
            return ent
    return ""  # dom: 或空 -> 不参与跨源聚类


def build_clusters(items: list[Item], cfg: dict[str, Any]) -> list[Cluster]:
    if not cfg.get("enabled", True):
        return [Cluster(id=sha1(it.id)[:12], key=f"solo:{it.id}", items=[it]) for it in items]

    window = timedelta(hours=float(cfg.get("window_hours", 72)))
    min_sim = float(cfg.get("min_title_similarity", 0.25))
    max_members = int(cfg.get("max_members", 12))

    groups: dict[str, list[Item]] = {}
    solo: list[Item] = []
    for item in items:
        key = _merge_key(item)
        if key:
            groups.setdefault(key, []).append(item)
        else:
            solo.append(item)

    clusters: list[Cluster] = []
    for key, group in groups.items():
        group.sort(key=lambda it: it.published_at or "")
        buckets: list[dict[str, Any]] = []  # {items, tokens, last_dt}
        for item in group:
            toks = tokenize(item.title)
            dt = published_dt(item)
            placed = False
            for bucket in buckets:
                if len(bucket["items"]) >= max_members:
                    continue
                if (dt - bucket["last_dt"]).total_seconds() > window.total_seconds():
                    continue
                if jaccard(toks, bucket["tokens"]) < min_sim and len(bucket["items"]) > 1:
                    continue
                if jaccard(toks, bucket["tokens"]) < min_sim and len(bucket["items"]) == 1:
                    # 只有一条时也要求最低相似度，避免同实体的不同事件被强行合并
                    if jaccard(toks, bucket["tokens"]) < min_sim * 0.6:
                        continue
                bucket["items"].append(item)
                bucket["tokens"] |= toks
                bucket["last_dt"] = dt
                placed = True
                break
            if not placed:
                buckets.append({"items": [item], "tokens": set(toks), "last_dt": dt})

        for bucket in buckets:
            members = bucket["items"]
            first = members[0].published_at or ""
            cid = sha1(f"{key}|{first}|{members[0].id}")[:12]
            for m in members:
                m.cluster_id = cid
            clusters.append(Cluster(id=cid, key=key, items=members, first_seen=first))

    for item in solo:
        cid = sha1(f"solo:{item.id}")[:12]
        item.cluster_id = cid
        clusters.append(Cluster(id=cid, key=f"solo:{item.id}", items=[item],
                                first_seen=item.published_at or ""))

    clusters.sort(key=lambda c: c.first_seen)
    return clusters
