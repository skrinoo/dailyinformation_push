"""主编排：一次运行 = 一条九段链路（可选两段收件侧加工）。

显式顺序，不允许乱：
  plan(AI) → fetch → normalize → watermark → dedupe(L1) → triage(AI/标题级)
        → cluster(L2) → coarse → judge(四闸) → quota
        → [studio 选题] → [feishu_task 待办] → render → push → persist

AI 只出现在 plan（决定搜什么）、triage（丢掉明显无关）、judge（打分与表达）
以及可选的 studio（选题与大纲），其余全部是确定性代码。
预算在 plan 之前建立，所有 AI 节点共用同一本账。

每一步都返回「留下了多少、砍掉了多少、为什么」，全部落 trace。
没有这一步，用户问「今天怎么只有一条」时只能靠猜。

多关注项：一次调用只跑一路，topic 作为参数传进来，
水位线、已知 URL、收录记录都按 topic 分区（见 watermark.py / store.py）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import (aiplan, autosource, dedupe, feishu_task, quota, render, studio,
               watermark)
from .budget import TokenBudget
from .cluster import build_clusters
from .coarse import coarse_rank
from .config import load_config
from .i18n import resolve_lang, t
from .judge import judge_all
from .models import CHANNEL_ARCHIVE, VERDICT_REJECTED, Item
from .normalize import normalize_item
from .push import dispatch, send_heartbeat
from .sources import fetch_all
from .store import Store
from .topics import effective as topic_effective
from .trace import Tracer, new_trace_id


def _load_fixtures(path: str | Path) -> list[dict[str, Any]]:
    """离线样本支持 age_hours：每次运行把时间重基准到「几小时前」，
    这样固定样本不会被水位线判成旧数据，也让示例长期可复现。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or []
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        if "age_hours" in row:
            hours = float(row.pop("age_hours"))
            row["published_at"] = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
        out.append(row)
    return out


def _history_for(items: list[Item], store: Store) -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for ent in item.entities[:1]:
            if ent in history:
                continue
            history[ent] = store.event_history(ent, limit=3)
    return history


def _enrich_also(clusters: list[Any], picked_ids: set[str]) -> None:
    for cluster in clusters:
        for member in cluster.items:
            if member.id in picked_ids:
                member.also_reported = cluster.also(member.id)


def run_once(
    cfg: dict[str, Any] | None = None,
    store: Store | None = None,
    *,
    topic: str = "",
    dry_run: bool = False,
    offline: bool = False,
    fixtures: str = "tests/fixtures/sample_items.json",
    force_rule: bool = False,
    limit: int | None = None,
    reset_watermark: bool = False,
    refresh_plan: bool = False,
    verbose: bool = True,
    tokens_used_today: int | None = None,
    log_fn: Any = None,
) -> dict[str, Any]:
    cfg = cfg or load_config("config.json")
    topic_id = str(topic or cfg.get("topic_id") or "")
    if topic_id:
        cfg = topic_effective(cfg, topic_id)
    storage = cfg["storage"]
    own_store = store is None
    store = store or Store(storage["db"])

    run_id = new_trace_id()
    tracer = Tracer(storage.get("trace_dir", "traces"), run_id,
                    enabled=bool(cfg["observability"].get("trace_enabled", True)))
    started = datetime.now()
    store.start_run(run_id, topic_id)

    if reset_watermark:
        store.conn.execute("DELETE FROM watermark")
        store.conn.commit()

    # 控制台会把 log_fn 指到自己的行缓冲；CLI 仍走 print
    log = log_fn or ((lambda *a: print(*a)) if verbose else (lambda *a: None))
    result: dict[str, Any] = {"run_id": run_id, "errors": []}
    lang = resolve_lang(cfg)
    tz_name = cfg["schedule"].get("timezone", "Asia/Shanghai")

    # ---------- 预算（贯穿 plan / triage / judge 三处 AI 调用）----------
    used_today = tokens_used_today if tokens_used_today is not None else store.tokens_today(tz_name)
    budget = TokenBudget.from_config(cfg, used_today=used_today)
    api_key = os.environ.get(cfg["llm"].get("api_key_env") or "LLM_API_KEY", "").strip()
    if force_rule:
        api_key = ""  # --no-llm 同时关掉采集侧 AI，保证完全确定性的复现

    # ---------- 0. AI 检索策略（采集前）----------
    plan = aiplan.get_plan(cfg, store, api_key, budget=budget, tracer=tracer,
                           force=refresh_plan)
    result["plan"] = {
        "source": plan.get("source"),
        "queries": plan.get("queries") or [],
        "rationale": plan.get("rationale", ""),
    }
    log(f"[0/9] 检索策略（{plan.get('source')}）：{len(plan.get('queries') or [])} 条查询"
        f" · {budget.describe()}")

    # ---------- 1. 采集 ----------
    offline_items = _load_fixtures(fixtures) if offline else None

    # 四类源全空时才自动选源：用户配了任何一个源，就不猜他想要什么。
    if offline_items is None and autosource.needs_discovery(cfg):
        auto = autosource.ensure(cfg, api_key, budget=budget, tracer=tracer)
        feeds = [f for f in (auto.get("feeds") or []) if f.get("url")]
        if feeds:
            cfg = {**cfg, "sources": {**cfg["sources"],
                                      "rss": [*(cfg["sources"].get("rss") or []), *feeds]}}
            result["auto_sources"] = {
                "source": auto.get("source"),
                "feeds": [{"name": f.get("name"), "url": f.get("url"),
                           "tier": f.get("tier"), "items": f.get("validated_items")}
                          for f in feeds],
                "rejected": len(auto.get("rejected") or []),
            }
            log(f"[0/9] 未配置采集源，自动选了 {len(feeds)} 个："
                + "、".join(str(f.get("name")) for f in feeds))
        else:
            log("[0/9] 未配置采集源，自动选源也没有可用结果（候选全部被拒或抓不通）")

    raw_items, errors = fetch_all(cfg, store, tracer, offline_items, plan=plan)
    result["errors"] = errors
    result["fetched"] = len(raw_items)
    log(f"[1/9] 采集 {len(raw_items)} 条" + (f"，{len(errors)} 个源异常" if errors else ""))

    if not raw_items:
        store.finish_run(run_id, fetched=0, kept=0, pushed=0, status="empty",
                         note="所有源均无返回或全部失败", tokens=budget.used_run)
        return _finish(cfg, store, tracer, run_id, result, [], [], started, dry_run, own_store, budget)

    # ---------- 2. 归一化 ----------
    with tracer.span("normalize", count=len(raw_items)) as p:
        items = [normalize_item(it.to_dict()) for it in raw_items]
        p["unique_urls"] = len({i.canonical_url for i in items})
    log(f"[2/9] 归一化完成，{len({i.canonical_url for i in items})} 个唯一 URL")

    # ---------- 3. 水位线 ----------
    fresh, wm_audit = watermark.filter_new(items, store, cfg, ignore_history=reset_watermark, topic=topic_id)
    result["after_watermark"] = len(fresh)
    result["watermark_skipped"] = len(wm_audit)
    log(f"[3/9] 水位线过滤：保留 {len(fresh)}，跳过 {len(wm_audit)}")

    if not fresh:
        store.finish_run(run_id, fetched=len(raw_items), kept=0, pushed=0, status="empty",
                         note="水位线之后无新条目", tokens=budget.used_run)
        return _finish(cfg, store, tracer, run_id, result, [], [], started, dry_run, own_store, budget)

    # ---------- 4. L1 去重 ----------
    deduped, dedupe_audit = dedupe.dedupe(fresh, cfg["dedupe"])
    result["after_dedupe"] = len(deduped)
    result["dedupe_folded"] = len(dedupe_audit)
    log(f"[4/9] L1 去重：{len(fresh)} → {len(deduped)}（折叠 {len(dedupe_audit)}）")

    # ---------- 5. AI 标题级初筛 ----------
    # 放在水位线与去重之后：只对「新且未重复」的条目花 token。
    # 只丢明显无关，不打分、不排序；丢弃项仍入库，避免下轮重新抓取。
    deduped, triage_dropped, triage_stats = aiplan.triage_items(
        deduped, cfg, api_key, budget=budget, tracer=tracer
    )
    result["triage"] = triage_stats
    result["after_triage"] = len(deduped)
    if triage_stats.get("enabled"):
        log(f"[5/9] AI 初筛：检查 {triage_stats['checked']}，丢弃 {triage_stats['dropped']}"
            f"（{triage_stats['batches']} 批，跳过 {triage_stats['skipped']}）")
    else:
        log("[5/9] AI 初筛未启用（或无可用模型）")

    if not deduped:
        for item in fresh:
            store.save_item(item.to_dict(), topic_id)
        store.finish_run(run_id, fetched=len(raw_items), after_dedupe=len(fresh),
                         clustered=0, gated=0, kept=0, pushed=0, status="empty",
                         note="AI 初筛后无候选", tokens=budget.used_run)
        if not dry_run:
            watermark.advance(fresh, store, topic_id)
        return _finish(cfg, store, tracer, run_id, result, [], [], started, dry_run, own_store, budget)

    # ---------- 6. L2 事件簇 ----------
    with tracer.span("cluster", count=len(deduped)) as p:
        clusters = build_clusters(deduped, cfg["cluster"])
        p["clusters"] = len(clusters)
        p["multi_member"] = sum(1 for c in clusters if len(c.items) > 1)
    result["clusters"] = len(clusters)
    log(f"[6/9] 事件簇：{len(clusters)} 个（其中 {sum(1 for c in clusters if len(c.items) > 1)} 个多来源）")

    candidates = [c.representative for c in clusters]
    # 先把「另有 N 家报道」补齐，后面 confidence 校正要用这个信号
    _enrich_also(clusters, {c.representative.id for c in clusters})

    # ---------- 7. 粗排 ----------
    weights = store.source_weights(topic_id)
    shortlist, blocked = coarse_rank(candidates, cfg, weights)
    result["coarse_kept"] = len(shortlist)
    result["blocked"] = len(blocked)
    log(f"[7/9] 粗排：{len(candidates)} → {len(shortlist)}（屏蔽词砍掉 {len(blocked)}）")

    if not shortlist:
        for item in fresh:
            store.save_item(item.to_dict(), topic_id)
        store.finish_run(run_id, fetched=len(raw_items), after_dedupe=len(deduped),
                         clustered=len(clusters), gated=0, kept=0, pushed=0,
                         status="empty", note="粗排后无候选", tokens=budget.used_run)
        if not dry_run:
            watermark.advance(fresh, store, topic_id)
        return _finish(cfg, store, tracer, run_id, result, [], [], started, dry_run, own_store, budget)

    # ---------- 8. 四闸判定 ----------
    history = _history_for(shortlist, store)
    with tracer.span("judge", count=len(shortlist)) as p:
        judge_stats = judge_all(shortlist, cfg, history, weights, tracer=tracer,
                                api_key=api_key, force_rule=force_rule, budget=budget)
        p.update(judge_stats)
    result["judge"] = judge_stats
    store.set_run_tokens(run_id, budget.used_run)
    passed = [it for it in shortlist if it.verdict != VERDICT_REJECTED]
    result["gated"] = len(passed)
    log(f"[8/9] 四闸：{len(shortlist)} → {len(passed)} 通过"
        + (f"（LLM {judge_stats['llm_ok']} 条，规则 {judge_stats['rule_only']} 条，"
           f"失败批次 {judge_stats['llm_failed']}）" if cfg["llm"].get("enabled") else "（规则模式）"))

    # ---------- 9. 配额 ----------
    quota_result = quota.apply_quota(passed, cfg)
    picked: list[Item] = quota_result["picked"]
    if limit is not None:
        for extra in picked[limit:]:
            extra.channel = CHANNEL_ARCHIVE
        picked = picked[:limit]
    result["quota"] = {k: v for k, v in quota_result.items() if k != "picked"}
    result["kept"] = len(picked)
    log(f"[9/9] 配额：入选 {len(picked)} 条（单源上限 {quota_result['per_source_cap']}）")

    _enrich_also(clusters, {it.id for it in shortlist})

    # ---------- 落库 ----------
    # 关键：入库的是「全部处理过的条目」，不是「入选条目」。
    # 只存入围的话，被 L1 折叠的转载、被簇合并掉的第二家报道，
    # 下一轮都不在已知 URL 里，会重新走完整链路——水位线永远收不紧。
    shortlist_ids = {it.id for it in shortlist}
    coarse_cut_ids = {c.representative.id for c in clusters} - shortlist_ids
    for item in fresh:
        if item.id in shortlist_ids:
            pass
        elif item.id in coarse_cut_ids and not item.verdict:
            item.verdict = "coarse_cut"
            item.reject_reason = "coarse_rank"
        elif not item.verdict:
            item.verdict = "processed"
            item.reject_reason = item.reject_reason or "folded_into_another_cluster"
        store.save_item(item.to_dict(), topic_id)
    for rec in blocked:
        tracer.event("coarse.blocked", item_id=rec["id"], title=rec["title"], reason=rec["reason"])

    for item in picked:
        if item.entities:
            store.upsert_event(
                f"{item.entities[0]}::{item.cluster_id}", item.entities[0],
                item.title, len(item.also_reported) + 1,
            )
    # 关注项维度的收录记录：简报与 topics history 都读这张表
    for item in picked:
        store.save_topic_item(topic_id, item, run_id)
    store.mark_seen([it.canonical_url for it in fresh], topic_id)

    # ---------- 10. 静默判定 ----------
    # 「没有值得打扰的东西」和「没有东西」是两件事：
    # 后者靠零产出心跳，前者靠这一条——不推送，但不是坏了。
    silence = (cfg["filter"].get("silence") or {})
    min_conf = float(silence.get("min_confidence", 0.55))
    top_conf = max((it.confidence for it in picked), default=0.0)
    silenced = bool(silence.get("enabled", True)) and bool(picked) and top_conf < min_conf
    result["silenced"] = silenced
    result["top_confidence"] = round(top_conf, 3)
    if silenced:
        log(f"  静默：最高置信 {top_conf:.2f} < {min_conf:.2f}，本轮不推送，留到晚间简报")

    # ---------- 11. 收件侧加工：选题 ----------
    pack = None
    if not silenced:
        pack = studio.build_pack(picked, cfg, api_key=api_key, budget=budget, tracer=tracer)
    result["pack"] = ({"item_id": pack.get("item_id"), "source": pack.get("source"),
                       "sections": len(pack.get("outline") or [])} if pack else None)
    if pack:
        log(f"  选题：{pack.get('source')} · {len(pack.get('outline') or [])} 段")

    # ---------- 12. 待办写入 ----------
    tasks: list[dict[str, Any]] = []
    if picked and not silenced:
        tasks = feishu_task.dispatch(cfg, picked, store, lang, tracer=tracer)
        created = sum(1 for r in tasks if r.get("ok") and r.get("count"))
        if tasks:
            log(f"  待办：建 {created} 条" + (f"，失败 {len(tasks) - created}" if len(tasks) > created else ""))
    result["tasks"] = tasks

    # ---------- 渲染与投递 ----------
    title = render.digest_title(lang, started)
    if cfg.get("topic_name"):
        title = f"{title} · {cfg['topic_name']}"
    out_dir = Path(storage.get("out_dir", "out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    # 淘汰明细同时包含「判定淘汰」与「AI 初筛丢弃」，否则报告看不出噪声去哪了
    rejected = [it for it in shortlist if it.reject_reason] + triage_dropped[:20]

    md_path = out_dir / f"digest-{stamp}.md"
    html_path = out_dir / f"digest-{stamp}.html"
    md_path.write_text(render.markdown(picked, title, lang, pack=pack), encoding="utf-8")
    html_path.write_text(render.html_digest(picked, rejected, title, lang, pack=pack),
                         encoding="utf-8")
    result["md"] = str(md_path)
    result["html"] = str(html_path)

    if silenced:
        result["push"] = [{"channel": "silenced", "ok": True,
                           "detail": f"最高置信 {top_conf:.2f} 低于 {min_conf:.2f}，未推送",
                           "count": 0}]
    elif dry_run:
        result["push"] = [{"channel": "dry-run", "ok": True, "detail": "未实际发送", "count": len(picked)}]
    else:
        result["push"] = dispatch(cfg, picked, store, run_id, title, tracer=tracer,
                                  pack=pack, tasks=tasks)

    # ---------- 零产出心跳 ----------
    result["heartbeat"] = []
    if not picked:
        threshold = int(cfg["channels"].get("heartbeat", {}).get("after_empty_runs", 2))
        empty_streak = store.consecutive_empty_runs() + 1
        if cfg["channels"].get("heartbeat", {}).get("enabled") and empty_streak >= threshold:
            text = render.heartbeat_text(len(raw_items), cfg,
                                         reason=t("heartbeat_reason", lang),
                                         lang=lang)
            result["heartbeat"] = ([] if dry_run else send_heartbeat(cfg, text, tracer))
            log(f"  心跳已触发（连续 {empty_streak} 次零产出）")

    result["rejected_sample"] = [
        {"title": it.title[:50], "reason": it.reject_reason, "gates": it.gates}
        for it in rejected[:5]
    ]

    note_bits = []
    if errors:
        note_bits.append(f"{len(errors)} 源异常")
    if silenced:
        note_bits.append("silenced")
    store.finish_run(run_id, fetched=len(raw_items), after_dedupe=len(deduped),
                     clustered=len(clusters), gated=len(passed), kept=len(picked),
                     pushed=sum(1 for p in result["push"] if p.get("ok") and p.get("count")),
                     status="ok", note="；".join(note_bits),
                     cost_usd=judge_stats.get("cost_usd", 0.0),
                     tokens=budget.used_run)
    # 水位线只推进到「处理过的」条目，未处理的下一轮还能进来。
    # dry-run 是预览：不能吃掉水位线，否则「先 --dry-run 看报告，再去掉它真推」
    # 这条 README 流程会永远推不出东西——真跑时全是「已看过」。
    if not dry_run:
        watermark.advance(fresh, store, topic_id)

    return _finish(cfg, store, tracer, run_id, result, picked, rejected, started, dry_run,
                   own_store, budget)


def _finish(cfg, store, tracer, run_id, result, picked, rejected, started, dry_run, own_store,
            budget: TokenBudget | None = None) -> dict[str, Any]:
    result["elapsed_s"] = round((datetime.now() - started).total_seconds(), 2)
    result["trace"] = str(tracer.dump() or "")
    result["trace_summary"] = tracer.summary()
    if budget is not None:
        result["budget"] = budget.summary()
    if own_store:
        store.close()
    return result
