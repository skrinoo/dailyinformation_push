"""没配置采集源时，自动挑源。

触发条件很窄：GitHub / HN / RSS / 页面监控四类源全部为空或全关。用户只要配了任何一个源，
这个模块就不介入——自动挑源是兜底，不是替代用户的选择。

三条硬约束：
1. **只从可信域名里挑**。选源比写稿更容易出错：一个 SEO 农场 feed 能让整份日报变成垃圾。
   所以模型给的 URL 必须落在 `DOMAIN_TIERS` 白名单里（政府/监管/官方博客/顶级会议/主流媒体），
   白名单外的直接拒，并把理由记下来。要放开得显式开 `sources.auto.allow_unknown`。
2. **不验不采信**。每个候选都必须真实抓一次、解析出条目才算数；404、坏 XML、空 feed 一律拒。
   模型说「这是官方 RSS」不作数，HTTP 200 + 能解析出 item 才算。
3. **结果可缓存、可审计**。按关注范围指纹缓存（默认 7 天），命中缓存不再调模型；
   选了什么、拒了什么、为什么，都写进 data/auto_sources.json，控制台和 CLI 都能看。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .budget import BudgetExceeded, TokenBudget
from .llm import LLMError, chat, parse_json_loose
from .sources import rss as rss_source
from .util import clip, http_get_text

DEFAULTS = {
    "enabled": True,
    "max_feeds": 6,
    "ttl_hours": 168,
    "allow_unknown": False,
    "validate": True,
    "timeout": 15,
}

# tier 1 = 一手/官方（政府、监管、公司官方博客、预印本、标准组织）
# tier 2 = 有编辑流程的主流媒体
# tier 3 = 社区聚合（信噪比低，但突发事件最快）
CATALOG: list[dict[str, Any]] = [
    # ---------------- 金融 / 宏观 ----------------
    {"name": "华尔街见闻", "url": "https://dedicated.wallstreetcn.com/rss.xml",
     "tier": 2, "lang": "zh",
     "tags": ["金融", "财经", "宏观", "市场", "股票", "投资", "利率", "货币", "经济", "债券", "黄金"]},
    {"name": "CNBC 财经", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
     "tier": 2, "lang": "en",
     "tags": ["金融", "财经", "市场", "股票", "美股", "投资", "经济", "finance", "market"]},
    {"name": "WSJ 市场", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
     "tier": 2, "lang": "en",
     "tags": ["金融", "市场", "股票", "美股", "债券", "finance", "market"]},
    {"name": "BBC 商业", "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
     "tier": 2, "lang": "en",
     "tags": ["金融", "商业", "经济", "公司", "business", "economy"]},
    # ---------------- AI / 学术 ----------------
    {"name": "Hugging Face 博客", "url": "https://huggingface.co/blog/feed.xml",
     "tier": 1, "lang": "en",
     "tags": ["ai", "大模型", "开源模型", "llm", "模型", "机器学习", "agent"]},
    {"name": "arXiv cs.AI", "url": "http://export.arxiv.org/rss/cs.AI",
     "tier": 1, "lang": "en",
     "tags": ["论文", "学术", "ai", "机器学习", "llm", "研究", "paper"]},
    {"name": "Google DeepMind", "url": "https://deepmind.google/blog/rss.xml",
     "tier": 1, "lang": "en",
     "tags": ["ai", "大模型", "研究", "deepmind", "多模态"]},
    {"name": "OpenAI 新闻", "url": "https://openai.com/news/rss.xml",
     "tier": 1, "lang": "en", "tags": ["ai", "大模型", "gpt", "openai", "模型发布"]},
    # ---------------- 工程 / 开源 ----------------
    {"name": "Kubernetes 博客", "url": "https://kubernetes.io/feed.xml",
     "tier": 1, "lang": "en",
     "tags": ["云原生", "kubernetes", "容器", "运维", "发布", "release"]},
    {"name": "GitHub 博客", "url": "https://github.blog/feed/",
     "tier": 1, "lang": "en",
     "tags": ["开源", "github", "开发工具", "ci/cd", "代码", "ai 编码"]},
    {"name": "Cloudflare 博客", "url": "https://blog.cloudflare.com/rss/",
     "tier": 1, "lang": "en",
     "tags": ["网络", "安全", "cdn", "dns", "性能", "infra"]},
    {"name": "Hacker News 头条", "url": "https://hnrss.org/frontpage",
     "tier": 3, "lang": "en",
     "tags": ["技术", "创业", "开源", "编程", "ai", "startup"]},
    {"name": "36氪", "url": "https://36kr.com/feed",
     "tier": 2, "lang": "zh",
     "tags": ["创业", "融资", "产品", "商业", "科技", "ai", "消费"]},
    # ---------------- 医疗 / 政策 ----------------
    {"name": "WHO 新闻", "url": "https://www.who.int/rss-feeds/news-english.xml",
     "tier": 1, "lang": "en", "tags": ["医疗", "健康", "公共卫生", "药品", "who"]},
    {"name": "NIH 新闻", "url": "https://www.nih.gov/news-events/news-releases/rss.xml",
     "tier": 1, "lang": "en", "tags": ["医疗", "健康", "科研", "药品", "临床"]},
]

# 白名单：只认这些域名（含子域）。tier 决定权重，不在于排序而在于「允不允许进」。
DOMAIN_TIERS: dict[str, int] = {
    # 官方 / 一手
    "openai.com": 1, "anthropic.com": 1, "deepmind.google": 1,
    "huggingface.co": 1, "arxiv.org": 1, "kubernetes.io": 1, "github.blog": 1,
    "cloudflare.com": 1, "nvidia.com": 1, "microsoft.com": 1, "googleblog.com": 1,
    "who.int": 1, "nih.gov": 1, "europa.eu": 1, "nsf.gov": 1,
    "gov.cn": 1, "pbc.gov.cn": 1, "stats.gov.cn": 1, "nsfc.gov.cn": 1,
    "moe.gov.cn": 1, "nmpa.gov.cn": 1, "csrc.gov.cn": 1,
    # 主流媒体
    "wallstreetcn.com": 2, "cnbc.com": 2, "wsj.com": 2, "dj.com": 2,
    "bbc.co.uk": 2, "bbci.co.uk": 2, "ft.com": 2,
    "reuters.com": 2, "ft.com": 2, "bloomberg.com": 2, "caixin.com": 2,
    "cls.cn": 2, "yicai.com": 2, "stcn.com": 2, "36kr.com": 2,
    "techcrunch.com": 2, "theverge.com": 2, "arstechnica.com": 2,
    "technologyreview.com": 2, "nature.com": 2, "science.org": 2,
    # 社区
    "hnrss.org": 3, "news.ycombinator.com": 3,
}

TIER_WEIGHT = {1: 1.2, 2: 1.0, 3: 0.85}
TIER_LABEL = {1: "官方", 2: "主流媒体", 3: "社区"}

SUSPICIOUS_HOST = re.compile(
    r"(?:^|\.)(?:xyz|top|shop|vip|cc|buzz|click|link|info|online|site|website|"
    r"live|space|fun|icu|cyou|gq|tk|ml|ga|cf)$", re.I)


# ---------------------------------------------------------------- 触发判断
def enabled_source_count(cfg: dict[str, Any]) -> int:
    src = cfg.get("sources") or {}
    n = 0
    if (src.get("github") or {}).get("enabled"):
        n += 1
    if (src.get("hn") or {}).get("enabled"):
        n += 1
    n += sum(1 for f in (src.get("rss") or [])
             if isinstance(f, dict) and f.get("enabled") is not False and f.get("url"))
    page = src.get("page") or {}
    if page.get("enabled") and (page.get("targets") or []):
        n += 1
    return n


def needs_discovery(cfg: dict[str, Any]) -> bool:
    auto = {**DEFAULTS, **((cfg.get("sources") or {}).get("auto") or {})}
    if not auto["enabled"]:
        return False
    return enabled_source_count(cfg) == 0


# ---------------------------------------------------------------- 信任与校验
def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_tier(host: str) -> int:
    """返回 0 表示不在白名单。子域按父域算（blog.openai.com → openai.com）。"""
    if not host:
        return 0
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in DOMAIN_TIERS:
            return DOMAIN_TIERS[candidate]
    return 0


def trust_ok(url: str, allow_unknown: bool = False) -> tuple[bool, int, str]:
    host = _host(url)
    if not host:
        return False, 0, "URL 解析不出域名"
    if SUSPICIOUS_HOST.search(host):
        return False, 0, f"可疑域名 {host}"
    tier = domain_tier(host)
    if tier:
        return True, tier, ""
    if allow_unknown:
        return True, 3, f"不在白名单（{host}），按 allow_unknown 放行"
    return False, 0, f"不在可信域名白名单：{host}"


def validate_feed(feed: dict[str, Any],
                  fetch_text: Callable[..., str] = http_get_text,
                  timeout: int = 15, limit: int = 5) -> tuple[bool, int, str]:
    """真抓一次并解析。解析不出来就不算源——模型说它是官方 feed 也不作数。"""
    url = str(feed.get("url") or "")
    try:
        text = fetch_text(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, 0, f"抓取失败：{type(exc).__name__}: {clip(str(exc), 80)}"
    if not text or len(text) < 200:
        return False, 0, "响应过短，可能不是 feed"
    try:
        items = rss_source._parse(text, str(feed.get("name") or url),
                                  float(feed.get("weight") or 1.0), limit,
                                  str(feed.get("lang") or "en"))
    except Exception as exc:  # noqa: BLE001
        return False, 0, f"不是合法的 RSS/Atom：{type(exc).__name__}"
    if not items:
        return False, 0, "能解析但没有条目"
    return True, len(items), ""


# ---------------------------------------------------------------- 候选与打分
def _topic_text(cfg: dict[str, Any]) -> str:
    profile = cfg.get("profile") or {}
    parts = [
        str(profile.get("role") or ""),
        " ".join(str(x) for x in (profile.get("interests") or [])),
        " ".join(str(x) for x in (cfg["filter"].get("keywords") or [])),
        str(cfg.get("topic_name") or ""),
        str(profile.get("custom_instructions") or "")[:200],
    ]
    return " ".join(parts).lower()


def catalog_matches(cfg: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    """没模型时的确定性选择：按标签命中数 + tier 排序。"""
    text = _topic_text(cfg)
    scored: list[tuple[float, dict[str, Any]]] = []
    for entry in CATALOG:
        hits = sum(1 for tag in entry["tags"] if tag.lower() in text)
        if not hits:
            continue
        score = hits * 3 + (4 - entry["tier"]) + (0.5 if entry["lang"] == "zh" else 0)
        scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [dict(e) for _, e in scored[:limit]]


def _as_feed(entry: dict[str, Any], tier: int, validated: int, why: str = "") -> dict[str, Any]:
    return {
        "name": entry.get("name") or _host(entry.get("url", "")) or "auto",
        "url": entry["url"],
        "lang": entry.get("lang") or "en",
        "weight": round(TIER_WEIGHT.get(tier, 1.0), 2),
        "limit": int(entry.get("limit") or 10),
        "auto": True,
        "tier": tier,
        "trust": TIER_LABEL.get(tier, "未分类"),
        "validated_items": validated,
        "why": clip(why, 40),
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- 模型选源
SYSTEM_PROMPT = """你是采集源规划器。用户还没有配置任何资讯源，需要你从候选清单里挑几个能覆盖其关注范围的 feed。

铁律：
1. 优先候选清单里的 URL，不要改写成别的地址。
2. 清单确实不够时，最多补 2 个你认为一手、权威的官方 RSS（政府/监管/公司官方博客/顶级会议/主流媒体），
   并在 why 里说明它为什么权威。禁止推荐聚合站、论坛、内容农场、需要登录的页面。
3. 只输出 JSON。

输出格式：
{"feeds":[{"url":"https://...","name":"站点名","why":"不超过 20 字的理由"}]}"""


def _prompt(cfg: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    profile = cfg.get("profile") or {}
    lines = [
        "## 用户关注",
        f"角色：{profile.get('role') or '未填写'}",
        f"主题：{cfg.get('topic_name') or '默认'}",
        f"关注：{'、'.join(profile.get('interests') or []) or '未填写'}",
        f"检索词：{'、'.join((cfg['filter'].get('keywords') or [])[:12]) or '未填写'}",
        f"语言：{profile.get('output_language') or 'zh'}",
        "",
        "## 候选清单",
    ]
    for entry in candidates:
        lines.append(f"- {entry['name']} | {entry['url']} | tier={entry['tier']} "
                     f"| 标签：{'、'.join(entry['tags'][:6])}")
    lines.append("")
    lines.append(f"从中挑 3-{len(candidates)} 个（不够可补 1-2 个官方源）。")
    return "\n".join(lines)


def discover(
    cfg: dict[str, Any],
    api_key: str = "",
    *,
    budget: TokenBudget | None = None,
    tracer: Any = None,
    use_llm: bool = True,
    allow_unknown: bool | None = None,
    max_feeds: int | None = None,
    fetch_text: Callable[..., str] = http_get_text,
    chat_fn: Callable[..., Any] = chat,
    validate: bool = True,
    timeout: int = 15,
) -> dict[str, Any]:
    """挑源 → 信任过滤 → 真抓校验 → 排序截断。返回 {feeds, rejected, source, tokens}。"""
    auto_cfg = {**DEFAULTS, **((cfg.get("sources") or {}).get("auto") or {})}
    allow_unknown = auto_cfg["allow_unknown"] if allow_unknown is None else allow_unknown
    cap = int(max_feeds or auto_cfg["max_feeds"])
    validate = bool(auto_cfg["validate"]) if validate else False
    timeout = int(auto_cfg.get("timeout") or timeout)

    candidates = catalog_matches(cfg, limit=max(6, cap))
    proposed: list[dict[str, Any]] = []
    source = "catalog"
    tokens = 0

    if use_llm and api_key and (cfg.get("llm") or {}).get("enabled"):
        try:
            res = chat_fn(cfg, api_key,
                          [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": _prompt(cfg, candidates)}],
                          json_mode=True, budget=budget, stage="autosource")
            data = parse_json_loose(res.text)
            rows = data.get("feeds") if isinstance(data, dict) else None
            for row in rows or []:
                if isinstance(row, dict) and row.get("url"):
                    proposed.append({"url": str(row["url"]).strip(),
                                     "name": str(row.get("name") or "").strip(),
                                     "why": str(row.get("why") or "").strip()})
            if proposed:
                source = "llm"
            tokens = int(getattr(res, "total_tokens", 0) or 0)
        except (LLMError, BudgetExceeded) as exc:
            if tracer:
                tracer.event("autosource.llm_failed", error=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001  解析失败也回落，不拖垮采集
            if tracer:
                tracer.event("autosource.llm_failed", error=f"{type(exc).__name__}: {exc}"[:200])

    # 模型没给或不可用时，用目录匹配兜底
    if not proposed:
        proposed = [{"url": e["url"], "name": e["name"], "why": "目录匹配"}
                    for e in candidates]
        source = "catalog" if source != "llm" else "catalog+llm"

    # 模型给的 URL 可能不在目录里，名字与语言要回填
    by_url = {e["url"]: e for e in CATALOG}
    seen: set[str] = set()
    feeds: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for row in proposed:
        url = row["url"]
        if url in seen:
            continue
        seen.add(url)
        ok, tier, reason = trust_ok(url, allow_unknown=allow_unknown)
        if not ok:
            rejected.append({"url": url, "reason": reason})
            continue
        entry = dict(by_url.get(url) or {})
        entry.update({"url": url, "name": row.get("name") or entry.get("name") or _host(url)})
        entry.setdefault("lang", "zh" if re.search(r"[\u4e00-\u9fff]", entry["name"]) else "en")

        count = 0
        if validate:
            ok, count, reason = validate_feed(entry, fetch_text=fetch_text, timeout=timeout)
            if not ok:
                rejected.append({"url": url, "reason": reason})
                continue
        feeds.append(_as_feed(entry, tier, count, row.get("why", "")))
        if len(feeds) >= cap:
            break

    feeds.sort(key=lambda f: (f["tier"], -f["validated_items"]))
    return {"feeds": feeds[:cap], "rejected": rejected, "source": source,
            "tokens": tokens, "candidates": len(candidates)}


# ---------------------------------------------------------------- 缓存
def _cache_path(cfg: dict[str, Any]) -> Path:
    db = Path((cfg.get("storage") or {}).get("db") or "data/intel.db")
    return db.parent / "auto_sources.json"


def fingerprint(cfg: dict[str, Any]) -> str:
    profile = cfg.get("profile") or {}
    payload = {
        "topic": cfg.get("topic_id") or "",
        "role": profile.get("role") or "",
        "interests": profile.get("interests") or [],
        "keywords": cfg["filter"].get("keywords") or [],
        "lang": profile.get("output_language") or "zh",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"version": 1, "entries": {}}


def cache_path(cfg: dict[str, Any]) -> Path:
    return _cache_path(cfg)


def load_cache(cfg: dict[str, Any]) -> dict[str, Any]:
    return _load_cache(_cache_path(cfg))


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # 缓存写不进去不影响本轮使用


def ensure(
    cfg: dict[str, Any],
    api_key: str = "",
    *,
    budget: TokenBudget | None = None,
    tracer: Any = None,
    force: bool = False,
    fetch_text: Callable[..., str] = http_get_text,
    chat_fn: Callable[..., Any] = chat,
) -> dict[str, Any]:
    """带缓存的选源。命中缓存直接返回，不调模型、不发请求。"""
    auto_cfg = {**DEFAULTS, **((cfg.get("sources") or {}).get("auto") or {})}
    path = _cache_path(cfg)
    key = fingerprint(cfg)
    cache = _load_cache(path)
    ttl = float(auto_cfg["ttl_hours"]) * 3600
    entry = (cache.get("entries") or {}).get(key)

    if entry and not force:
        age = time.time() - float(entry.get("created_at") or 0)
        if age < ttl:
            if tracer:
                tracer.event("autosource.cache_hit", feeds=len(entry.get("feeds") or []))
            return {**entry, "source": "cache", "cached": True}

    result = discover(cfg, api_key, budget=budget, tracer=tracer,
                      fetch_text=fetch_text, chat_fn=chat_fn)
    cache.setdefault("entries", {})[key] = {
        "created_at": time.time(),
        "created_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topic": cfg.get("topic_name") or cfg.get("topic_id") or "默认",
        "feeds": result["feeds"],
        "rejected": result["rejected"],
        "source": result["source"],
    }
    _save_cache(path, cache)
    return result


# ---------------------------------------------------------------- 落到配置
def apply_to_config(cfg: dict[str, Any], feeds: list[dict[str, Any]]) -> dict[str, Any]:
    """把选中的源追加进 config.sources.rss（按 URL 去重，不改动已有源）。"""
    out = json.loads(json.dumps(cfg, ensure_ascii=False))
    rss_list = list(out["sources"].get("rss") or [])
    have = {str(f.get("url") or "").rstrip("/") for f in rss_list if isinstance(f, dict)}
    for feed in feeds:
        url = str(feed.get("url") or "").rstrip("/")
        if not url or url in have:
            continue
        rss_list.append({k: v for k, v in feed.items()
                         if k in ("name", "url", "lang", "weight", "limit", "auto",
                                  "tier", "trust", "why", "added_at")})
        have.add(url)
    out["sources"]["rss"] = rss_list
    return out
