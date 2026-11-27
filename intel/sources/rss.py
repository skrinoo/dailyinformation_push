from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from ..models import Item
from ..util import clip, http_get_text, parse_datetime, sha1, strip_html

ATOM = "{http://www.w3.org/2005/Atom}"

# hnrss 的 description 对无正文的帖子只包含这四行元数据，
# 直接当摘要用会让日报里出现「Article URL: ... Points: 52」这种噪音。
_META_LINE = re.compile(
    r"^(article url|comments url|points|#\s*comments|comments?)\s*:\s*.*$", re.I
)


def clean_description(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").split("\n")]
    kept = [ln for ln in lines if ln and not _META_LINE.match(ln)]
    if kept:
        return "\n".join(kept)
    # 全是元数据时，至少把标题信息留下来，不要给用户看空壳
    return ""


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _parse(xml_text: str, feed_name: str, weight: float, limit: int,
           lang: str = "en") -> list[Item]:
    root = ET.fromstring(xml_text)
    items: list[Item] = []

    # RSS 2.0 / RDF
    nodes = root.findall(".//item")
    if nodes:
        for node in nodes[:limit]:
            title = _text(node.find("title"))
            link = _text(node.find("link"))
            if not link:
                guid = node.find("guid")
                link = _text(guid)
            if not title or not link:
                continue
            desc = _text(node.find("description")) or _text(node.find("{http://purl.org/rss/1.0/modules/content/}encoded"))
            published = _text(node.find("pubDate")) or _text(node.find("{http://purl.org/dc/elements/1.1/}date"))
            items.append(
                Item(
                    id=sha1(link)[:16],
                    title=strip_html(title),
                    url=link,
                    source=feed_name,
                    published_at=published,
                    raw_summary=clip(clean_description(strip_html(desc)), 600),
                    meta={"feed_weight": weight, "lang": lang},
                )
            )
        return items

    # Atom
    for node in root.findall(f".//{ATOM}entry")[:limit]:
        title = _text(node.find(f"{ATOM}title"))
        link = ""
        for link_node in node.findall(f"{ATOM}link"):
            if link_node.get("rel") in (None, "", "alternate"):
                link = link_node.get("href", "")
                break
        if not title or not link:
            continue
        desc = _text(node.find(f"{ATOM}summary")) or _text(node.find(f"{ATOM}content"))
        published = _text(node.find(f"{ATOM}published")) or _text(node.find(f"{ATOM}updated"))
        items.append(
            Item(
                id=sha1(link)[:16],
                title=strip_html(title),
                url=link,
                source=feed_name,
                published_at=published,
                raw_summary=clip(clean_description(strip_html(desc)), 600),
                meta={"feed_weight": weight, "lang": lang},
            )
        )
    return items


def _guess_lang(name: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", name or "") else "en"


def fetch_one(feed: dict[str, Any]) -> list[Item]:
    """抓单个 feed。异常向上抛，由编排层做单源隔离与告警。"""
    url = (feed or {}).get("url", "")
    if not url:
        return []
    name = feed.get("name") or url
    xml_text = http_get_text(
        url,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
        timeout=int(feed.get("timeout", 25)),
    )
    return _parse(xml_text, name, float(feed.get("weight", 1.0)), int(feed.get("limit", 10)),
                  str(feed.get("lang") or _guess_lang(name)))


def fetch(cfg: dict[str, Any]) -> list[Item]:
    out: list[Item] = []
    for feed in cfg["sources"].get("rss") or []:
        url = (feed or {}).get("url", "")
        if not url:
            continue
        name = feed.get("name") or url
        try:
            out.extend(fetch_one(feed))
        except Exception as exc:  # a dead feed must not kill the run
            print(f"  [warn] rss {name}: {exc}")
    return out


def _age_hours(item: Item) -> float:
    dt = parse_datetime(item.published_at)
    if not dt:
        return 9999.0
    from ..util import hours_since

    return hours_since(dt)
