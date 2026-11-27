"""采集阶段的 AI 辅助。

AI 介入两个位置，且只在这两个位置：

  ① 采集前 —— 检索策略规划（plan）
     把「用户关注范围」翻译成可执行的查询：GitHub 的 topic/keyword + 语言、
     HN Algolia 的检索词。解决的是「配置里只有几个词，覆盖不到用户真正想看的东西」。
     结果按 TTL 缓存；用户改了关注范围，指纹变化会立即失效。

  ② 采集后 —— 标题级初筛（triage）
     只判断「明显无关」，不打分、不排序、不淘汰「相关但质量一般」的条目。
     目的是把娱乐/招聘/空投这类噪声在进入去重和判定之前就丢掉，省下后面的 token。

两条硬约束，都是防止模型越权：
  - 规划输出必须过结构化校验：topic 只允许 [a-z0-9-]，language 必须在允许列表，
    query 去掉引号与换行。模型给不出合规查询就回落到配置里的确定性查询。
  - 初筛的丢弃比例上限 35%（可配），超过就整批保留。宁可多留噪声，
    也不能让一次模型抖动把当日该看的东西全删掉。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .budget import BudgetExceeded, TokenBudget
from .llm import LLMError, chat, parse_json_loose
from .models import CHANNEL_ARCHIVE, Item
from .util import clip

PLAN_VERSION = 2
MAX_DROP_RATIO = 0.35

# GitHub 搜索接口接受的语言名是固定枚举，不接受任意字符串。
# 白名单而不是黑名单：模型输出「C++/Rust」这种拼接值会直接搜不到。
LANGUAGE_ALLOW = {
    "c", "c++", "c#", "python", "rust", "go", "java", "typescript", "javascript",
    "zig", "swift", "kotlin", "ruby", "lua", "shell", "haskell", "ocaml", "elixir",
}
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
KEYWORD_RE = re.compile(r"^[\w+#.\- ]{2,60}$", re.UNICODE)
# 检索词会被拼进第三方 API 的 query 参数。模型偶尔会把「指令」当成检索词写进来，
# 这类字符串既搜不到东西，也没必要发出去——直接判为非法。
SUSPICIOUS_QUERY = re.compile(
    r"(ignore\s+(all\s+)?(previous|above)|disregard|system\s*prompt|"
    r"忽略.{0,6}(指令|要求|提示)|你现在是|you\s+are\s+now)", re.I)


def fingerprint(cfg: dict[str, Any]) -> str:
    """关注范围一变，缓存就要失效。只取会影响检索结果的字段。"""
    profile = cfg.get("profile") or {}
    payload = {
        "v": PLAN_VERSION,
        "interests": profile.get("interests") or [],
        "avoid": profile.get("avoid") or [],
        "lang": profile.get("output_language") or "zh",
        "instructions": profile.get("custom_instructions") or "",
        "gh_topics": (cfg["sources"].get("github") or {}).get("topics") or [],
        "gh_langs": (cfg["sources"].get("github") or {}).get("languages") or [],
        "rss": [f.get("name") for f in (cfg["sources"].get("rss") or [])],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fallback_plan(cfg: dict[str, Any]) -> dict[str, Any]:
    """没有模型时的确定性方案：配置里写了什么就用什么。"""
    gh = cfg["sources"].get("github") or {}
    topics = [str(t).lower() for t in (gh.get("topics") or []) if TOPIC_RE.match(str(t).lower())]
    langs = [str(l) for l in (gh.get("languages") or []) if str(l).lower() in LANGUAGE_ALLOW]
    queries: list[dict[str, Any]] = []
    for i, topic in enumerate(topics[:4]):
        queries.append({
            "kind": "github", "topic": topic,
            "language": langs[0] if len(langs) == 1 else (langs[i % len(langs)] if langs else ""),
            "why": "配置中的 GitHub 主题",
        })
    for interest in (cfg["profile"].get("interests") or [])[:3]:
        # 没有模型时无法翻译，只能把中文关注项里的英文片段抽出来当检索词；
        # 抽不出两个以上英文词就跳过——拿中文去搜 HN 只会得到空结果。
        ascii_part = re.sub(r"[^\x20-\x7E]+", " ", str(interest))
        # 「OpenWrt / 网络」剥掉中文会剩下「OpenWrt /」，纯符号算不上检索词
        words = [w for w in re.split(r"\s+", ascii_part) if re.search(r"[A-Za-z0-9]", w)]
        ascii_part = " ".join(words).strip()
        if len(words) >= 2 and len(ascii_part) <= 60:
            queries.append({"kind": "hn", "query": ascii_part, "min_points": 20,
                            "why": "由关注方向直接检索"})
    for q in ((cfg["sources"].get("hn") or {}).get("queries") or []):
        queries.append({"kind": "hn", "query": str(q), "min_points": 20, "why": "配置中的检索词"})

    # 去重 + 截断，与模型路径遵守同一个上限
    max_queries = int((cfg.get("ai") or {}).get("max_queries") or 6)
    uniq: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for row in queries:
        key = (row["kind"], str(row.get("topic") or row.get("query") or "").lower(),
               str(row.get("language") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)
    return {"queries": uniq[:max_queries], "rationale": "fallback: 未调用模型，直接使用配置",
            "source": "fallback"}


def _clean_query(text: str) -> str:
    text = re.sub(r"[\"'`\n\r\t]", " ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return clip(text, 80)


def sanitize_plan(raw: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """把模型输出收敛成可执行、可审计的查询列表。非法项直接丢，不猜。"""
    if not isinstance(raw, dict):
        return fallback_plan(cfg)
    max_queries = int((cfg.get("ai") or {}).get("max_queries") or 6)
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    for row in raw.get("queries") or []:
        if not isinstance(row, dict) or len(out) >= max_queries:
            break
        kind = str(row.get("kind") or "").lower()
        why = clip(str(row.get("why") or ""), 60)

        if kind == "github":
            topic = str(row.get("topic") or "").strip().lower()
            lang = str(row.get("language") or "").strip()
            keywords = _clean_query(row.get("keywords") or "")
            if not (TOPIC_RE.match(topic) if topic else False) and not keywords:
                continue
            if keywords and (not KEYWORD_RE.match(keywords) or SUSPICIOUS_QUERY.search(keywords)):
                continue
            if lang and lang.lower() not in LANGUAGE_ALLOW:
                lang = ""
            key = ("github", topic, lang.lower(), keywords)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "kind": "github", "topic": topic, "language": lang,
                "keywords": keywords, "new_only": bool(row.get("new_only")), "why": why,
            })

        elif kind == "hn":
            query = _clean_query(row.get("query"))
            if len(query) < 2 or not re.search(r"[A-Za-z\u4e00-\u9fff]", query):
                continue
            if SUSPICIOUS_QUERY.search(query):
                continue
            try:
                points = int(row.get("min_points") or 20)
            except (TypeError, ValueError):
                points = 20
            points = max(5, min(500, points))
            key = ("hn", query.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": "hn", "query": query, "min_points": points, "why": why})

    if not out:
        return fallback_plan(cfg)
    rationale = clip(str(raw.get("rationale") or ""), 300)
    return {"queries": out, "rationale": rationale, "source": "llm"}


PLAN_SYSTEM = """你是检索策略规划器。你的输出会被程序直接拼进 GitHub Search 与 HN Algolia 的请求，
所以要具体、可执行、不重复。

规则：
1. 只输出 JSON，不要解释。
2. queries 给 3-6 条，覆盖用户关注范围的不同侧面，不要用同义词凑数。
3. GitHub 条目：topic 只能用小写字母、数字、连字符（如 llm-agent）；language 只能从允许列表选；
   也可以改用 keywords（普通检索词，会搜 name/description/readme），二者至少给一个。
4. HN 条目：query 用英文检索词（该库以英文内容为主），min_points 在 5-500 之间。
5. 用户「回避」的主题不要出现在查询里。
6. why 写这条查询想捞到什么，≤ 30 字。

输出格式：
{"queries":[{"kind":"github","topic":"llm-agent","language":"Python","keywords":"","new_only":false,"why":"..."},
            {"kind":"hn","query":"LLM agent framework","min_points":20,"why":"..."}],
 "rationale":"整体策略一句话"}"""


def build_plan(
    cfg: dict[str, Any],
    api_key: str,
    budget: TokenBudget | None = None,
    tracer: Any = None,
) -> dict[str, Any]:
    profile = cfg.get("profile") or {}
    ai_cfg = cfg.get("ai") or {}
    allowed = ", ".join(sorted(LANGUAGE_ALLOW))
    user = "\n".join([
        "## 用户关注",
        f"角色：{profile.get('role') or '未填写'}",
        f"关注：{'、'.join(profile.get('interests') or []) or '未填写'}",
        f"回避：{'、'.join(profile.get('avoid') or []) or '无'}",
        f"附加要求：{clip(profile.get('custom_instructions') or '无', 400)}",
        "",
        "## 已有源（不要重复这些词）",
        "GitHub 主题：" + "、".join((cfg["sources"].get("github") or {}).get("topics") or []),
        "RSS：" + "、".join(f.get("name", "") for f in (cfg["sources"].get("rss") or [])),
        "",
        f"## 约束\nlanguage 允许值：{allowed}\n最多 {int(ai_cfg.get('max_queries') or 6)} 条查询。",
    ])
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        result = chat(cfg, api_key, messages, json_mode=True, budget=budget, stage="ai.plan")
        plan = sanitize_plan(parse_json_loose(result.text), cfg)
        plan["tokens"] = result.total_tokens
        if tracer:
            tracer.event("ai.plan", queries=len(plan["queries"]), source=plan.get("source"),
                         tokens=result.total_tokens)
        return plan
    except (LLMError, BudgetExceeded) as exc:
        if tracer:
            tracer.event("ai.plan_failed", error=str(exc)[:200])
        plan = fallback_plan(cfg)
        plan["error"] = str(exc)[:200]
        return plan


def get_plan(
    cfg: dict[str, Any],
    store: Any,
    api_key: str,
    budget: TokenBudget | None = None,
    tracer: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """缓存 → 模型 → 回落，三级。缓存键含关注范围指纹，改了配置立刻重建。"""
    ai_cfg = cfg.get("ai") or {}
    if not ai_cfg.get("enabled") or not ai_cfg.get("plan_queries"):
        plan = fallback_plan(cfg)
        plan["source"] = "config"
        return plan

    key = fingerprint(cfg)
    ttl = int(ai_cfg.get("query_ttl_hours") or 24)
    if not force and store is not None:
        cached = store.get_plan_cache(key, ttl_hours=ttl)
        if cached:
            cached["source"] = "cache"
            if tracer:
                tracer.event("ai.plan_cache_hit", queries=len(cached.get("queries") or []))
            return cached

    if not api_key or not cfg["llm"].get("enabled"):
        plan = fallback_plan(cfg)
        plan["source"] = "config"
        return plan

    plan = build_plan(cfg, api_key, budget=budget, tracer=tracer)
    if store is not None and plan.get("queries"):
        store.save_plan_cache(key, plan)
    return plan


# ---------------------------------------------------------------- 标题级初筛
TRIAGE_SYSTEM = """你是采集层的粗筛器，只做一件事：丢掉「明显与用户关注领域无关」的条目。

不要评价质量、不要打分、不要因为「看起来不重要」而丢。判断标准：
- 明显无关：娱乐八卦、体育、纯促销、加密货币空投、招聘广告、与关注领域毫无关系的泛科技新闻。
- 拿不准就保留。漏掉一条真新闻的代价，远大于多留一条噪声。
- 只输出 JSON。

输出格式：
{"drop":[{"id":"候选id","why":"不超过 12 字的理由"}]}
不需要丢弃时输出 {"drop":[]}"""


def _triage_prompt(batch: list[Item], cfg: dict[str, Any]) -> str:
    profile = cfg.get("profile") or {}
    lines = [
        "## 关注",
        f"角色：{profile.get('role') or '未填写'}",
        f"关注：{'、'.join(profile.get('interests') or []) or '未填写'}",
        f"回避：{'、'.join(profile.get('avoid') or []) or '无'}",
        "",
        "## 候选",
    ]
    for item in batch:
        lines.append(f'<candidate id="{item.id}">')
        lines.append(f"source: {item.source}")
        lines.append(f"title: {clip(item.title, 160)}")
        if item.raw_summary:
            lines.append(f"excerpt: {clip(item.raw_summary, 120)}")
        lines.append("</candidate>")
    return "\n".join(lines)


def triage_items(
    items: list[Item],
    cfg: dict[str, Any],
    api_key: str,
    budget: TokenBudget | None = None,
    tracer: Any = None,
) -> tuple[list[Item], list[Item], dict[str, Any]]:
    """返回 (保留, 丢弃, 统计)。丢弃项会被打上 verdict，便于回查。"""
    ai_cfg = cfg.get("ai") or {}
    stats: dict[str, Any] = {"enabled": False, "checked": 0, "dropped": 0,
                             "batches": 0, "capped": 0, "skipped": 0}
    if not (ai_cfg.get("enabled") and ai_cfg.get("triage")):
        return items, [], stats
    if not api_key or not cfg["llm"].get("enabled"):
        return items, [], stats

    stats["enabled"] = True
    batch_size = max(10, int(ai_cfg.get("triage_batch_size") or 40))
    keyword_hits = {k.lower() for k in (cfg["filter"].get("keywords") or []) if k}
    keep: list[Item] = []
    dropped: list[Item] = []

    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        try:
            result = chat(cfg, api_key,
                          [{"role": "system", "content": TRIAGE_SYSTEM},
                           {"role": "user", "content": _triage_prompt(batch, cfg)}],
                          json_mode=True, budget=budget, stage="ai.triage")
        except BudgetExceeded:
            stats["skipped"] += len(batch)
            keep.extend(batch)
            continue
        except LLMError as exc:
            stats["skipped"] += len(batch)
            keep.extend(batch)
            if tracer:
                tracer.event("ai.triage_error", error=str(exc)[:200], batch=start)
            continue

        stats["batches"] += 1
        stats["checked"] += len(batch)
        try:
            rows = parse_json_loose(result.text).get("drop") or []
        except (LLMError, AttributeError):
            rows = []
        reasons = {str(r.get("id")): clip(str(r.get("why") or ""), 40)
                   for r in rows if isinstance(r, dict)}

        # 比例上限：模型大范围丢弃时整批保留，只记事件。
        cap = max(1, int(len(batch) * MAX_DROP_RATIO))
        if len(reasons) > cap:
            stats["capped"] += 1
            keep.extend(batch)
            if tracer:
                tracer.event("ai.triage_capped", batch=start, wanted=len(reasons), cap=cap)
            continue

        for item in batch:
            why = reasons.get(item.id)
            if not why:
                keep.append(item)
                continue
            # 反向保护：命中两个以上关注词的条目不由初筛决定去留
            text = f"{item.title} {item.raw_summary}".lower()
            if sum(1 for k in keyword_hits if k in text) >= 2:
                keep.append(item)
                continue
            item.verdict = "triage_dropped"
            item.reject_reason = f"ai_triage:{why or 'off_topic'}"
            item.channel = CHANNEL_ARCHIVE
            dropped.append(item)
            stats["dropped"] += 1

    if tracer:
        tracer.event("ai.triage", **stats)
    return keep, dropped, stats
