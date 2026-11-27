"""Page Monitor：盯住没有 RSS 的页面（定价页、changelog、release notes）。

原理是基线 + 文本 diff：首次抓取只存基线不产出条目，之后每次比较哈希，
哈希变了才做行级 diff，把「新增了什么」作为条目正文。

这类源的价值在于：模型定价、版本说明、竞品功能页的变化，
通常不会出现在任何 RSS 里，但恰恰是最需要第一时间知道的情报。
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Item
from ..util import clip, http_get_text, sha1, strip_html

_WS = re.compile(r"[ \t\u3000]+")


def _clean(html_text: str, max_bytes: int) -> str:
    text = strip_html(html_text)
    text = _WS.sub(" ", text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out = "\n".join(lines)
    return out[:max_bytes]


def diff_lines(old: str, new: str, limit: int = 12) -> str:
    """极简行级 diff：只报新增与删除，不追求 LCS 最优。"""
    old_set = set(old.split("\n"))
    new_set = set(new.split("\n"))
    added = [ln for ln in new.split("\n") if ln not in old_set][:limit]
    removed = [ln for ln in old.split("\n") if ln not in new_set][:limit]
    parts = []
    if added:
        parts.append("新增：" + " / ".join(clip(a, 80) for a in added))
    if removed:
        parts.append("移除：" + " / ".join(clip(r, 80) for r in removed))
    return clip("；".join(parts) or "页面内容有变化", 900)


def fetch(page_cfg: dict[str, Any], store: Any) -> list[Item]:
    items: list[Item] = []
    max_bytes = int(page_cfg.get("max_bytes", 2_000_000))

    for target in page_cfg.get("targets", []):
        url = (target or {}).get("url", "")
        if not url:
            continue
        name = target.get("name") or url
        try:
            html_text = http_get_text(url, timeout=int(target.get("timeout", 30)))
        except Exception:
            continue  # 单页失败不影响其他页

        text = _clean(html_text, max_bytes)
        digest = sha1(text)
        prev = store.get_page_baseline(url)

        if prev is None:
            store.save_page_baseline(url, digest, text)
            continue
        if prev["digest"] == digest:
            continue

        change = diff_lines(prev["text"], text)
        store.save_page_baseline(url, digest, text)
        # 用 ?v=<digest> 保证每次变更的 canonical_url 唯一，同时不污染原页面链接
        items.append(Item(
            id=sha1(f"{url}#{digest}")[:16],
            title=f"{name} 有变更",
            url=f"{url}?v={digest[:8]}",
            source="page",
            raw_summary=change,
            meta={"page_url": url, "is_change": True, "baseline_digest": digest},
        ))
    return items
