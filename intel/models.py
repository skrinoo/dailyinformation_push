"""v2 数据模型。

相比 v1，Item 多了四个判断力字段：entities（事件簇主键）、gates（四闸分项）、
confidence（下游分流依据）、verdict（放行/淘汰 + 淘汰原因）。这些字段全部落库，
保证「为什么推了 / 为什么没推」可回查，而不需要重新问一次模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 四闸的固定维度名。改名会破坏已落库的历史数据，故集中在此处定义。
GATE_NAMES = ("relevance", "importance", "novelty", "actionability")

VERDICT_SELECTED = "selected"
VERDICT_REJECTED = "rejected"

CHANNEL_IMMEDIATE = "immediate"
CHANNEL_DAILY = "daily"
CHANNEL_ARCHIVE = "archive"


@dataclass
class Item:
    """一条归一化后的候选。id 由 canonical_url 派生，跨源稳定。"""

    id: str
    title: str
    url: str
    canonical_url: str = ""
    source: str = ""
    published_at: str = ""
    raw_summary: str = ""
    # 生成结果
    headline: str = ""     # 一行说清发生了什么；为空时回退到原标题
    summary: str = ""
    points: list[str] = field(default_factory=list)  # 2-3 条可核验要点，卡片正文用它
    reason: str = ""       # 已废弃：仅供历史库与 trace 排查，不再渲染给用户
    change_type: str = ""
    tags: list[str] = field(default_factory=list)
    # 判断结果
    entities: list[str] = field(default_factory=list)
    cluster_id: str = ""
    gates: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    confidence: float = 0.0
    verdict: str = ""
    reject_reason: str = ""
    channel: str = ""
    # 溯源与折叠
    sources: list[str] = field(default_factory=list)
    also_reported: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == VERDICT_SELECTED

    @property
    def shown_title(self) -> str:
        """给用户看的标题：模型改写过就用改写版，否则用原标题。"""
        return (self.headline or self.title or "").strip()

    def gate_min(self) -> tuple[str, float]:
        """返回最低的那一维，用于解释淘汰原因。"""
        if not self.gates:
            return ("", 0.0)
        name = min(self.gates, key=lambda k: self.gates[k])
        return (name, self.gates[name])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "source": self.source,
            "published_at": self.published_at,
            "raw_summary": self.raw_summary,
            "headline": self.headline,
            "summary": self.summary,
            "points": self.points,
            "reason": self.reason,
            "change_type": self.change_type,
            "tags": self.tags,
            "entities": self.entities,
            "cluster_id": self.cluster_id,
            "gates": self.gates,
            "score": round(self.score, 2),
            "confidence": round(self.confidence, 3),
            "verdict": self.verdict,
            "reject_reason": self.reject_reason,
            "channel": self.channel,
            "sources": self.sources or [self.source],
            "also_reported": self.also_reported,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            url=data.get("url", ""),
            canonical_url=data.get("canonical_url", ""),
            source=data.get("source", ""),
            published_at=data.get("published_at", ""),
            raw_summary=data.get("raw_summary", ""),
            headline=data.get("headline", ""),
            summary=data.get("summary", ""),
            points=list(data.get("points") or []),
            reason=data.get("reason", ""),
            change_type=data.get("change_type", ""),
            tags=list(data.get("tags") or []),
            entities=list(data.get("entities") or []),
            cluster_id=data.get("cluster_id", ""),
            gates={k: float(v) for k, v in (data.get("gates") or {}).items()},
            score=float(data.get("score") or 0.0),
            confidence=float(data.get("confidence") or 0.0),
            verdict=data.get("verdict", ""),
            reject_reason=data.get("reject_reason", ""),
            channel=data.get("channel", ""),
            sources=list(data.get("sources") or []),
            also_reported=list(data.get("also_reported") or []),
            meta=dict(data.get("meta") or {}),
        )


@dataclass
class Cluster:
    """L2 事件簇。一个簇只输出一条代表，其余折叠为 also_reported。"""

    id: str
    key: str
    items: list[Item] = field(default_factory=list)
    first_seen: str = ""

    @property
    def representative(self) -> Item:
        # 代表条目优先选：最早发布（首发原则），信息量更大者破平
        return min(self.items, key=lambda it: (it.published_at or "9999", -len(it.raw_summary)))

    def also(self, exclude_id: str) -> list[dict[str, str]]:
        return [
            {"source": it.source, "title": it.title, "url": it.url}
            for it in self.items
            if it.id != exclude_id
        ]
