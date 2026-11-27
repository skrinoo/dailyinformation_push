"""intel-agent CLI。

    python main.py init
    python main.py config show
    python main.py config set --focus "LLM agent, C++ 并发" --language zh --tokens-day 200000
    python main.py run --offline --dry-run --no-llm
    python main.py run --dry-run
    python main.py schedule
    python main.py feedback --id <id> --action up
    python main.py feedback --id <id> --action down --reason shallow
    python main.py stats
    python main.py feishu chats
    python main.py feishu test
    python main.py dingtalk test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from intel import feedback
from intel.config import backup_path, load_config, write_default
from intel.pipeline import run_once
from intel.scheduler import describe as describe_schedule
from intel.store import Store
from intel.util import now_local

ROOT = Path(__file__).resolve().parent


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def cmd_init(args: argparse.Namespace) -> int:
    path = write_default(args.config, force=args.force)
    print(f"配置已就绪：{path}")
    print("下一步：python main.py run --offline --dry-run --no-llm")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from intel.topics import enabled_ids, get_topic, list_topics

    targets: list[str]
    if args.topic and args.topic != "all":
        if get_topic(cfg, args.topic) is None:
            print(f"未知关注项：{args.topic}（可用：{', '.join(t['id'] or 'default' for t in list_topics(cfg))}）",
                  file=sys.stderr)
            return 2
        targets = [args.topic]
    elif len(cfg.get("topics") or []) > 0:
        targets = enabled_ids(cfg)
        if not targets:
            print("所有关注项都已停用。用 python main.py topics list 查看。", file=sys.stderr)
            return 2
    else:
        targets = [""]

    code = 0
    for topic_id in targets:
        print(f"===== 关注项 {topic_id or 'default'} =====")
        result = run_once(
            cfg,
            topic=topic_id,
            dry_run=args.dry_run,
            offline=args.offline,
            fixtures=args.fixtures,
            force_rule=args.no_llm,
            limit=args.limit,
            reset_watermark=args.reset_watermark,
            refresh_plan=args.refresh_plan,
        )
        _print_run(result)
        code = max(code, 0 if not result.get("errors") else 0)
    return code


def _print_run(result: dict) -> None:
    plan = result.get("plan") or {}
    triage = result.get("triage") or {}
    budget = result.get("budget") or {}
    print("-" * 60)
    print(f"run_id    {result['run_id']}")
    print(f"检索策略  {plan.get('source')} · {len(plan.get('queries') or [])} 条查询")
    for row in (plan.get("queries") or [])[:6]:
        if row.get("kind") == "github":
            bits = [f"topic:{row['topic']}" if row.get("topic") else "",
                    row.get("keywords", ""),
                    f"lang:{row['language']}" if row.get("language") else ""]
            print(f"          GH  {' '.join(b for b in bits if b)}")
        else:
            print(f"          HN  {row.get('query')} (>={row.get('min_points')} pts)")
    print("采集 → 水位 → 去重 → 初筛 → 簇 → 粗排 → 闸门 → 入选")
    print(f"  {result.get('fetched', 0)} → {result.get('after_watermark', 0)} → "
          f"{result.get('after_dedupe', 0)} → {result.get('after_triage', 0)} → "
          f"{result.get('clusters', 0)} → {result.get('coarse_kept', 0)} → "
          f"{result.get('gated', 0)} → {result.get('kept', 0)}")
    if triage.get("enabled"):
        print(f"AI 初筛   检查 {triage.get('checked', 0)} · 丢弃 {triage.get('dropped', 0)}"
              f" · 越权拦截 {triage.get('capped', 0)} 批 · 跳过 {triage.get('skipped', 0)}")
    if budget:
        left = budget.get("remaining")
        print(f"Token    本轮 {budget.get('used_run', 0)}"
              + (f" / {budget.get('per_run')}" if budget.get("per_run") else "")
              + f" · 今日 {budget.get('used_today', 0)}"
              + (f" / {budget.get('per_day')}" if budget.get("per_day") else "")
              + (f" · 剩余 {left}" if left is not None else ""))
    if result.get("silenced"):
        print(f"静默      最高置信 {result.get('top_confidence')} 未达打扰阈值，未推送")
    pack = result.get("pack")
    if pack:
        print(f"选题      {pack.get('source')} · {pack.get('sections')} 段")
    if result.get("tasks"):
        created = sum(1 for t in result["tasks"] if t.get("ok") and t.get("count"))
        failed = [t for t in result["tasks"] if not t.get("ok")]
        print(f"待办      建成 {created} 条" + (f" · 失败 {len(failed)}" if failed else ""))
        for row in failed[:2]:
            print(f"  - {row.get('detail', '')[:90]}")
    print(f"耗时      {result.get('elapsed_s')}s")
    if result.get("errors"):
        print(f"源异常    {len(result['errors'])} 个")
        for err in result["errors"][:3]:
            print(f"  - {err['source']}: {err['error'][:80]}")
    for p in result.get("push", []):
        flag = "OK " if p.get("ok") else "ERR"
        print(f"投递      [{flag}] {p['channel']}: {p['detail'][:70]}")
    if result.get("html"):
        print(f"报告      {result['html']}")
    if result.get("md"):
        print(f"Markdown  {result['md']}")
    if result.get("trace"):
        print(f"Trace     {result['trace']}")


def cmd_schedule(args: argparse.Namespace) -> int:
    from intel.briefing import run_briefing
    from intel.scheduler import describe_all, next_event

    cfg = load_config(args.config)
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    print(f"定时计划：{describe_all(cfg)}。Ctrl+C 退出。")
    while True:
        target, kind, topic_id = next_event(cfg)
        wait = max(1.0, (target - now_local(tz)).total_seconds())
        label = "晚间简报" if kind == "brief" else f"关注项 {topic_id or 'default'}"
        print(f"下次 {target.strftime('%m-%d %H:%M')} {label}（{int(wait)}s 后）")
        time.sleep(wait)
        cfg = load_config(args.config)  # 用户可能中途改了频率，下一轮按新配置算
        try:
            if kind == "brief":
                store = Store(cfg["storage"]["db"])
                try:
                    run_briefing(cfg, store, dry_run=args.dry_run)
                finally:
                    store.close()
            else:
                run_once(cfg, topic=topic_id, dry_run=args.dry_run,
                         force_rule=args.no_llm, offline=False)
        except Exception as exc:  # 定时任务绝不能因为一次失败就退出
            print(f"[error] {label} 失败：{exc}", file=sys.stderr)


def cmd_brief(args: argparse.Namespace) -> int:
    from intel.briefing import run_briefing

    cfg = load_config(args.config)
    store = Store(cfg["storage"]["db"])
    try:
        run_briefing(cfg, store, day=args.day, dry_run=args.dry_run,
                     push=not args.no_push)
    finally:
        store.close()
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = Store(cfg["storage"]["db"])

    if not args.id:
        rows = store.recent_items(limit=args.limit, topic=args.topic)
        store.close()
        if not rows:
            print("还没有收录过条目。先跑一轮：python main.py run")
            return 0
        from intel.topics import _pad  # CJK 按双宽对齐，否则中英混排列会错位

        print(f"{_pad('id', 14)}{_pad('主题', 14)}{_pad('来源', 12)}"
              f"{_pad('反馈', 8)}{_pad('原因', 16)}{_pad('权重', 7)}标题")
        for r in rows:
            weight = f"{r['source_weight']:.2f}"
            why = feedback.label(r.get("last_reason") or "") if r.get("last_action") == "down" else "-"
            print(f"{_pad(r['id'][:12], 14)}{_pad(r['topic'] or '-', 14)}"
                  f"{_pad((r['source'] or '')[:10], 12)}{_pad(r['last_action'] or '-', 8)}"
                  f"{_pad(why or '-', 16)}"
                  f"{_pad(weight, 7)}{r['title'][:34]}")
        print("\n记一次反馈：python main.py feedback --id <id> --action up|save")
        print("               python main.py feedback --id <id> --action down --reason "
              + "|".join(feedback.codes()))
        return 0

    if not args.action:
        store.close()
        print("缺少 --action（up / down / save / open）", file=sys.stderr)
        return 2
    try:
        out = store.add_feedback(args.id, args.action, topic=args.topic,
                                 reason=args.reason)
    except (KeyError, ValueError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = Store(cfg["storage"]["db"])
    rows = store.stats()
    runs = store.recent_runs(5)
    today = store.tokens_today(cfg["schedule"].get("timezone", "Asia/Shanghai"))
    detail = store.topic_weights(args.topic) if getattr(args, "topic", "") else []
    reasons = store.reason_stats(args.topic if getattr(args, "topic", "") else "")
    store.close()

    print("来源权重")
    if not rows:
        print("  （还没有全局反馈数据；反馈默认记到条目所属关注项）")
    for r in rows:
        print(f"  {r['source']:<20} weight {r['weight']:.2f}  shown {r['shown']:>3}  "
              f"up {r['up']}  down {r['down']}  open {r['opens']}")

    if getattr(args, "topic", ""):
        print(f"\n关注项 {args.topic} 的权重")
        if not detail:
            print("  （还没有这个关注项的反馈；反馈会默认记到条目所属关注项）")
        for d in detail:
            print(f"  {d['source']:<20} 有效 {d['effective_weight']:.2f}"
                  f" = 全局 {d['global_weight']:.2f} × 主题 {d['topic_weight']:.2f}"
                  f"   up {d['up']}  down {d['down']}")

    budget = cfg.get("budget") or {}
    cap = budget.get("tokens_per_day") or 0
    print(f"\n今日 token  {today}" + (f" / {cap}" if cap else "（未设上限）"))

    print("\n「为什么没用」（近 30 天）")
    if not reasons:
        print("  （还没有「没用」记录）")
    for r in reasons:
        print(f"  {r['label']:<16} {r['count']:>3} 次  {r['share'] * 100:>5.1f}%")

    print("\n最近运行")
    for run in runs:
        print(f"  {run['started_at'][:19]}  {(run.get('topic') or 'default'):<12}"
              f"fetched {run['fetched'] or 0:>3}  "
              f"kept {run['kept'] or 0:>2}  tokens {run['tokens'] or 0:>6}  "
              f"push {run['pushed'] or 0:>2}  {run['status']}"
              + (f"  {run['note']}" if run.get("note") else ""))
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    trace_dir = Path(cfg["storage"].get("trace_dir", "traces"))
    files = sorted(trace_dir.glob("run-*.json"))
    if not files:
        print("还没有 trace 文件")
        return 1
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    summary = data["summary"]
    print(f"trace_id {summary['trace_id']}  total {summary['total_ms']}ms  errors {summary['errors']}")
    for name, agg in summary["spans"].items():
        print(f"  {name:<22} count {agg['count']:>3}  {agg['ms']:>8.1f}ms  errors {agg['errors']}")
    return 0


# ---------------------------------------------------------------- 用户设置
def _split(value: str) -> list[str]:
    return [v.strip() for v in str(value).replace("，", ",").split(",") if v.strip()]


def cmd_config_show(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    profile = cfg.get("profile") or {}
    budget = cfg.get("budget") or {}
    ai = cfg.get("ai") or {}
    feishu = (cfg.get("channels") or {}).get("feishu") or {}

    print("关注范围")
    print(f"  角色      {profile.get('role') or '（未填）'}")
    print(f"  关注      {'、'.join(profile.get('interests') or []) or '（未填）'}")
    print(f"  回避      {'、'.join(profile.get('avoid') or []) or '（无）'}")
    print(f"  输出语言  {profile.get('output_language') or 'zh'}")
    custom = (profile.get("custom_instructions") or "").strip()
    print(f"  附加要求  {custom if custom else '（无）'}")

    print("\n预算（token）")
    print(f"  单轮上限  {budget.get('tokens_per_run') or '不限'}")
    print(f"  每日上限  {budget.get('tokens_per_day') or '不限'}")
    print(f"  超限行为  {budget.get('on_exceed') or 'degrade'}")

    print("\n调度")
    from intel.scheduler import describe_all

    if cfg.get("topics"):
        for line in describe_all(cfg).split("；"):
            print(f"  {line}")
    else:
        print(f"  {describe_schedule(cfg['schedule'])}")
    brief = (cfg.get("schedule") or {}).get("briefing") or {}
    print(f"  晚间简报  {'开' if brief.get('enabled') else '关'}"
          + (f" · {brief.get('time')}" if brief.get("enabled") else ""))

    from intel.topics import list_topics

    print("\n关注项")
    for topic in list_topics(cfg):
        print(f"  {topic['id'] or 'default':<14}{topic.get('name', ''):<12}"
              f"{'启用' if topic.get('enabled', True) else '停用':<4}"
              f"{describe_schedule(topic.get('schedule') or {})}")

    silence = (cfg.get("filter") or {}).get("silence") or {}
    creator = ((cfg.get("output") or {}).get("creator") or {})
    print("\n打扰控制")
    print(f"  静默阈值  {'开' if silence.get('enabled', True) else '关'}"
          f" · 最高置信低于 {silence.get('min_confidence', 0.55)} 时不推送")
    print(f"  选题大纲  {'开' if creator.get('enabled', True) else '关'}"
          f" · 最低分 {creator.get('min_score', 5.0)}")
    tasks_cfg = ((cfg.get("channels") or {}).get("feishu") or {}).get("tasks") or {}
    print(f"  飞书待办  {'开' if tasks_cfg.get('enabled') else '关'}"
          f" · actionability≥{tasks_cfg.get('min_actionability', 7.0)}"
          f" · 每轮≤{tasks_cfg.get('max_per_run', 2)}")

    print("\nAI 辅助")
    print(f"  总开关    {'开' if ai.get('enabled') else '关'}")
    print(f"  检索规划  {'开' if ai.get('plan_queries') else '关'}"
          f" · 缓存 {ai.get('query_ttl_hours') or 24}h · 最多 {ai.get('max_queries') or 6} 条")
    print(f"  标题初筛  {'开' if ai.get('triage') else '关'}"
          f" · 批大小 {ai.get('triage_batch_size') or 40}")

    print("\n渠道")
    print(f"  飞书      {'开' if feishu.get('enabled') else '关'} · mode={feishu.get('mode') or 'webhook'}"
          f" · receive_id={feishu.get('receive_id') or '（空）'}")
    dd = (cfg.get("channels") or {}).get("dingtalk") or {}
    print(f"  钉钉      {'开' if dd.get('enabled') else '关'}"
          f" · webhook={dd.get('webhook_env') or '（空）'}")
    tg = (cfg.get("channels") or {}).get("telegram") or {}
    print(f"  Telegram  {'开' if tg.get('enabled') else '关'}")

    print("\n数据源")
    gh = cfg["sources"].get("github") or {}
    hn = cfg["sources"].get("hn") or {}
    print(f"  GitHub    {'开' if gh.get('enabled') else '关'} · topics "
          f"{'、'.join(gh.get('topics') or []) or '（空）'}")
    print(f"  HN 搜索   {'开' if hn.get('enabled') else '关'} · >= {hn.get('min_points')} pts")
    feeds = cfg["sources"].get("rss") or []
    print(f"  RSS       {len(feeds)} 个：" + "、".join(f.get("name", "?") for f in feeds))
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    path = Path(args.config)
    # 先留存原始文本：校验失败时要原样回滚，不能拿「已修改」的内容当备份
    original = path.read_text(encoding="utf-8")
    raw = json.loads(original)

    def put(section: str, key: str, value) -> None:
        raw.setdefault(section, {})
        if not isinstance(raw[section], dict):
            raw[section] = {}
        raw[section][key] = value

    changed: list[str] = []
    if args.role is not None:
        put("profile", "role", args.role); changed.append("role")
    if args.focus is not None:
        put("profile", "interests", _split(args.focus)); changed.append("focus")
    if args.avoid is not None:
        put("profile", "avoid", _split(args.avoid)); changed.append("avoid")
    if args.language is not None:
        put("profile", "output_language", args.language); changed.append("language")
    if args.instructions is not None:
        put("profile", "custom_instructions", args.instructions); changed.append("instructions")
    if args.tokens_run is not None:
        put("budget", "tokens_per_run", args.tokens_run); changed.append("tokens_run")
    if args.tokens_day is not None:
        put("budget", "tokens_per_day", args.tokens_day); changed.append("tokens_day")
    if args.on_exceed is not None:
        put("budget", "on_exceed", args.on_exceed); changed.append("on_exceed")
    if args.mode is not None:
        put("schedule", "mode", args.mode); changed.append("schedule.mode")
    if args.times is not None:
        put("schedule", "times", _split(args.times)); changed.append("schedule.times")
    if args.interval is not None:
        put("schedule", "interval_minutes", args.interval); changed.append("schedule.interval")
    if args.weekdays is not None:
        put("schedule", "weekdays", [int(d) for d in _split(args.weekdays)]); changed.append("weekdays")
    if args.active_hours is not None:
        hours = _split(args.active_hours.replace("-", ","))
        put("schedule", "active_hours", hours); changed.append("active_hours")
    if args.max_items is not None:
        put("filter", "max_items_per_run", args.max_items); changed.append("max_items")
    if args.keywords is not None:
        put("filter", "keywords", _split(args.keywords)); changed.append("keywords")
    if args.channels is not None:
        wanted = set(_split(args.channels))
        for name in ("feishu", "dingtalk", "telegram"):
            raw.setdefault("channels", {}).setdefault(name, {})
            raw["channels"][name]["enabled"] = name in wanted
        changed.append("channels")
    if args.feishu_mode is not None:
        raw.setdefault("channels", {}).setdefault("feishu", {})["mode"] = args.feishu_mode
        changed.append("feishu.mode")
    if args.feishu_receive_id is not None:
        raw["channels"].setdefault("feishu", {})["receive_id"] = args.feishu_receive_id
        changed.append("feishu.receive_id")
    if args.ai is not None:
        put("ai", "enabled", args.ai == "on"); changed.append("ai")
    if args.triage is not None:
        put("ai", "triage", args.triage == "on"); changed.append("ai.triage")
    if args.silence is not None:
        put("filter", "silence", {**(raw.get("filter", {}).get("silence") or {}),
                                  "enabled": args.silence != "off",
                                  "min_confidence": float(args.silence)
                                  if args.silence not in ("on", "off") else 0.55})
        changed.append("filter.silence")
    if args.creator is not None:
        put("output", "creator", {**(raw.get("output", {}).get("creator") or {}),
                                 "enabled": args.creator == "on"})
        changed.append("output.creator")
    if args.briefing is not None:
        sched = raw.setdefault("schedule", {}).setdefault("briefing", {})
        sched["enabled"] = args.briefing == "on"
        changed.append("schedule.briefing")
    if args.briefing_time is not None:
        sched = raw.setdefault("schedule", {}).setdefault("briefing", {})
        sched["time"] = args.briefing_time
        sched["enabled"] = True
        changed.append("schedule.briefing.time")
    if args.feishu_tasks is not None:
        section = raw.setdefault("channels", {}).setdefault("feishu", {}).setdefault("tasks", {})
        section["enabled"] = args.feishu_tasks == "on"
        changed.append("feishu.tasks")

    if not changed:
        print("没有要修改的项。用 python main.py config show 查看当前设置。")
        return 0

    backup = backup_path(path)
    backup.write_text(original, encoding="utf-8")
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        cfg = load_config(path)
    except Exception as exc:
        path.write_text(original, encoding="utf-8")
        print(f"配置校验失败，已回滚：{exc}", file=sys.stderr)
        return 2
    print(f"已更新 {len(changed)} 项：{', '.join(changed)}")
    print(f"原配置备份：{backup}")
    if "focus" in changed or "instructions" in changed:
        print("提示：关注范围变化会让 AI 检索策略缓存自动失效，下一轮会重新规划。")
    print(f"当前调度：{describe_schedule(cfg['schedule'])}")
    return 0


# ---------------------------------------------------------------- 关注项
def _write_topics(path: Path, raw: dict, original: str) -> int:
    """写回并校验；校验失败原样回滚。和 config set 用的是同一套纪律。"""
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        load_config(path)
    except Exception as exc:
        path.write_text(original, encoding="utf-8")
        print(f"配置校验失败，已回滚：{exc}", file=sys.stderr)
        return 2
    return 0


def cmd_topics(args: argparse.Namespace) -> int:
    from intel.topics import describe as describe_topic, get_topic, list_topics

    cfg = load_config(args.config)
    action = args.action

    if action == "list":
        for topic in list_topics(cfg):
            print(describe_topic(topic))
        print("\n提示：python main.py topics show <id> 看指令；topics history <id> 看历史。")
        return 0

    if action == "show":
        topic = get_topic(cfg, args.id or "")
        if topic is None:
            print(f"没有这个关注项：{args.id}", file=sys.stderr)
            return 2
        print(f"id        {topic['id'] or 'default'}")
        print(f"名称      {topic.get('name')}")
        print(f"状态      {'启用' if topic.get('enabled', True) else '停用'}")
        print(f"调度      {describe_schedule(topic.get('schedule') or {})}")
        print(f"附加要求  {(topic.get('custom_instructions') or '（继承全局）').strip()}")
        print(f"关注      {'、'.join(topic.get('interests') or []) or '（继承全局）'}")
        print(f"关键词    {'、'.join(topic.get('keywords') or []) or '（继承全局）'}")
        return 0

    if action == "history":
        store = Store(cfg["storage"]["db"])
        try:
            rows = store.topic_runs(args.id or "", limit=args.limit)
        finally:
            store.close()
        if not rows:
            print("还没有这一路的运行记录。")
            return 0
        print(f"{'时间':<20}{'采集':>5}{'入选':>5}{'推送':>5}{'tokens':>8}  备注")
        for row in rows:
            print(f"{(row['started_at'] or '')[:19]:<20}{row['fetched'] or 0:>5}"
                  f"{row['kept'] or 0:>5}{row['pushed'] or 0:>5}{row['tokens'] or 0:>8}  "
                  f"{row['note'] or ''}")
        return 0

    # add / set / rm 走原始 JSON 编辑，避免把默认值写进用户配置
    path = Path(args.config)
    original = path.read_text(encoding="utf-8")
    raw = json.loads(original)
    topics = raw.setdefault("topics", [])
    if not isinstance(topics, list):
        print("配置里的 topics 不是数组，先手动修好。", file=sys.stderr)
        return 2

    if action == "rm":
        before = len(topics)
        raw["topics"] = [t for t in topics if str(t.get("id")) != args.id]
        if len(raw["topics"]) == before:
            print(f"没有这个关注项：{args.id}", file=sys.stderr)
            return 2
        code = _write_topics(path, raw, original)
        if not code:
            print(f"已删除关注项 {args.id}（它的历史仍留在库里，随时可查）")
        return code

    def put_fields(row: dict) -> None:
        if args.name:
            row["name"] = args.name
        if args.instructions is not None:
            row["custom_instructions"] = args.instructions
        if args.focus is not None:
            row["interests"] = _split(args.focus)
        if args.keywords is not None:
            row["keywords"] = _split(args.keywords)
        if args.language:
            row["output_language"] = args.language
        if args.times:
            row.setdefault("schedule", {})["mode"] = "times"
            row["schedule"]["times"] = _split(args.times)
        if args.interval:
            row.setdefault("schedule", {})["mode"] = "interval"
            row["schedule"]["interval_minutes"] = args.interval
        if args.weekdays:
            row.setdefault("schedule", {})["weekdays"] = [int(d) for d in _split(args.weekdays)]
        if args.enable:
            row["enabled"] = True
        if args.disable:
            row["enabled"] = False

    if action == "add":
        if not args.id:
            print("add 需要 --id（用于水位线与历史分区，如 ai-eng）", file=sys.stderr)
            return 2
        if any(str(t.get("id")) == args.id for t in topics):
            print(f"关注项已存在：{args.id}（用 topics set 修改）", file=sys.stderr)
            return 2
        row: dict = {"id": args.id}
        put_fields(row)
        topics.append(row)
        code = _write_topics(path, raw, original)
        if not code:
            print(f"已新增关注项 {args.id}。用 python main.py run --topic {args.id} 试跑。")
        return code

    # set
    target = next((t for t in topics if str(t.get("id")) == args.id), None)
    if target is None:
        print(f"没有这个关注项：{args.id}", file=sys.stderr)
        return 2
    put_fields(target)
    code = _write_topics(path, raw, original)
    if not code:
        print(f"已更新关注项 {args.id}。指令修改会让检索策略缓存自动失效。")
    return code


def cmd_feishu(args: argparse.Namespace) -> int:
    from intel import feishu

    cfg = load_config(args.config)
    if not feishu.configured(cfg):
        print("飞书未启用或凭据缺失。先执行：")
        print("  python main.py config set --channels feishu --feishu-mode webhook")
        print("  export FEISHU_WEBHOOK='https://open.feishu.cn/open-apis/bot/v2/hook/xxx'")
        return 2

    if args.action == "chats":
        chats = feishu.list_chats(cfg)
        if not chats:
            print("机器人不在任何群里。先把应用机器人拉进群。")
            return 1
        for chat in chats:
            print(f"  {chat['chat_id']}  {chat['name']}")
        print("\n把要投递的群 id 写进配置：")
        print("  python main.py config set --feishu-receive-id <chat_id>")
        return 0

    # test
    try:
        out = feishu.send_text(cfg, "intel-agent 连通性测试：收到这条说明飞书通道可用。")
        print(f"发送成功：{out.get('detail')}")
        return 0
    except Exception as exc:
        print(f"发送失败：{exc}", file=sys.stderr)
        return 1


def cmd_dingtalk(args: argparse.Namespace) -> int:
    from intel import dingtalk

    cfg = load_config(args.config)
    if not dingtalk.configured(cfg):
        print("钉钉未启用或凭据缺失。先执行：")
        print("  python main.py config set --channels feishu,dingtalk   # 会重设全部渠道开关")
        print("  $env:DINGTALK_WEBHOOK='https://oapi.dingtalk.com/robot/send?access_token=xxx'")
        print("  $env:DINGTALK_SECRET='SECxxx'   # 安全设置选「加签」时才需要")
        print("  想持久化：把上面两行写进系统环境变量，或直接设在启动服务的环境里")
        return 2

    try:
        dingtalk.send_markdown(
            cfg, "intel-agent 连通性测试",
            "### 钉钉通道可用\n\n收到这条说明 webhook 地址与加签都对。")
        print("发送成功：群里应该已经收到一条 markdown 消息")
        return 0
    except Exception as exc:
        print(f"发送失败：{exc}", file=sys.stderr)
        return 1


def cmd_sources(args: argparse.Namespace) -> int:
    """采集源：查看 / 自动选源 / 单个 feed 体检。"""
    import json as _json

    from intel import autosource
    from intel.topics import effective as topic_effective

    cfg = load_config(args.config)
    topic = getattr(args, "topic", "") or ""
    if topic:
        try:
            cfg = topic_effective(cfg, topic)
        except KeyError:
            print(f"未知关注项：{topic}", file=sys.stderr)
            return 2

    action = getattr(args, "action", "list")

    if action == "list":
        src = cfg["sources"]
        print("已配置的源")
        print(f"  GitHub      {'开' if (src.get('github') or {}).get('enabled') else '关'}")
        print(f"  HN          {'开' if (src.get('hn') or {}).get('enabled') else '关'}")
        feeds = src.get("rss") or []
        print(f"  RSS         {len(feeds)} 个")
        for f in feeds:
            tag = "（自动）" if f.get("auto") else ""
            print(f"      - {f.get('name') or '?'} · {f.get('url')}{tag}")
        page = src.get("page") or {}
        print(f"  页面监控    {'开' if page.get('enabled') else '关'}"
              f" · {len(page.get('targets') or [])} 个目标")
        auto = {**autosource.DEFAULTS, **(src.get("auto") or {})}
        print(f"\n兜底选源：{'开' if auto['enabled'] else '关'} · 最多 {auto['max_feeds']} 个"
              f" · 缓存 {auto['ttl_hours']} 小时")
        if autosource.needs_discovery(cfg):
            print("  当前没有任何可用源，下一轮会自动选源")
        cache = autosource._load_cache(autosource._cache_path(cfg))
        entries = list((cache.get("entries") or {}).values())
        if entries:
            latest = max(entries, key=lambda e: float(e.get("created_at") or 0))
            print(f"  上次自动选源（{latest.get('created_at_iso')}，"
                  f"{latest.get('source')}）：")
            for f in latest.get("feeds") or []:
                print(f"      - {f.get('name')} · {f.get('url')}")
            if latest.get("rejected"):
                print(f"      拒掉 {len(latest['rejected'])} 个候选")
        return 0

    if action == "test":
        url = getattr(args, "url", "") or ""
        if not url:
            print("用法：python main.py sources test <feed-url>", file=sys.stderr)
            return 2
        ok, tier, reason = autosource.trust_ok(
            url, allow_unknown=bool(getattr(args, "allow_unknown", False)))
        print(f"信任校验：{'通过' if ok else '拒绝'}"
              + (f" · tier={tier}" if ok else f" · {reason}"))
        ok2, count, why = autosource.validate_feed(
            {"url": url, "name": url, "lang": "zh"}, timeout=20)
        print(f"抓取解析：{'通过 · 解析出 %d 条' % count if ok2 else '失败 · ' + why}")
        return 0 if (ok and ok2) else 1

    # auto
    refresh = bool(getattr(args, "refresh", False))
    api_key = os.environ.get(cfg["llm"].get("api_key_env") or "LLM_API_KEY", "").strip()
    result = autosource.ensure(cfg, api_key, force=refresh, chat_fn=autosource.chat)
    feeds = result.get("feeds") or []
    print(f"选源方式：{result.get('source')}"
          + ("（缓存）" if result.get("cached") else ""))
    if not feeds:
        print("没有选出可用源。候选全部被拒，或抓取全部失败。")
        for row in (result.get("rejected") or [])[:8]:
            print(f"  - {row.get('url')} · {row.get('reason')}")
        return 1
    print(f"选出 {len(feeds)} 个源：")
    for f in feeds:
        print(f"  [{f.get('trust')}] {f.get('name')} · {f.get('url')}"
              f" · 已验 {f.get('validated_items')} 条")
    rejected = result.get("rejected") or []
    if rejected:
        print(f"拒掉 {len(rejected)} 个：")
        for row in rejected[:8]:
            print(f"  - {row.get('url')} · {row.get('reason')}")

    if getattr(args, "apply", False):
        merged = autosource.apply_to_config(load_config(args.config), feeds)
        path = Path(args.config)
        backup = backup_path(path)
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(_json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            load_config(path)
        except Exception as exc:  # noqa: BLE001
            backup.replace(path)
            print(f"配置校验失败，已回滚：{exc}", file=sys.stderr)
            return 2
        print(f"已写入 {path}（原文件备份在 {backup}）")
    else:
        print("只预览，没改配置。要写入就加 --apply。")

    print("\n关注项也能单独指定源：在 topics 里写 sources.rss，会自动覆盖全局。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="intel-agent", description="可定制 AI 情报推送智能体")
    p.add_argument("--config", default="config.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="生成默认配置")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="跑一轮（多关注项时默认跑全部启用项）")
    s.add_argument("--topic", default="", help="只跑某个关注项；all=全部启用项")
    s.add_argument("--dry-run", action="store_true", help="不实际发送")
    s.add_argument("--offline", action="store_true", help="用 fixtures 而不是真实源")
    s.add_argument("--fixtures", default="tests/fixtures/sample_items.json")
    s.add_argument("--no-llm", action="store_true", help="强制规则模式（同时关掉采集侧 AI）")
    s.add_argument("--limit", type=int, default=None, help="限制入选条数")
    s.add_argument("--reset-watermark", action="store_true", help="清空水位线重新处理历史")
    s.add_argument("--refresh-plan", action="store_true", help="忽略缓存，重新规划检索策略")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("schedule", help="常驻定时（每个关注项按自己的频率 + 晚间简报）")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-llm", action="store_true")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("brief", help="生成/补发晚间简报")
    s.add_argument("--day", default="today", help="today | yesterday | YYYY-MM-DD")
    s.add_argument("--dry-run", action="store_true", help="只生成文件，不发送")
    s.add_argument("--no-push", action="store_true", help="只生成文件")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("topics", help="管理关注项（每一路独立频率与要求）")
    ts = s.add_subparsers(dest="action", required=True)
    t_list = ts.add_parser("list", help="列出关注项")
    t_list.set_defaults(func=cmd_topics)
    t_show = ts.add_parser("show", help="查看某个关注项的指令")
    t_show.add_argument("id", nargs="?", default="")
    t_show.set_defaults(func=cmd_topics)
    t_hist = ts.add_parser("history", help="查看某个关注项的执行历史")
    t_hist.add_argument("id", nargs="?", default="")
    t_hist.add_argument("--limit", type=int, default=10)
    t_hist.set_defaults(func=cmd_topics)
    for name in ("add", "set"):
        t = ts.add_parser(name, help="新增/修改关注项")
        t.add_argument("--id", required=True)
        t.add_argument("--name")
        t.add_argument("--instructions", help="这一路的附加要求（作为可信用户指令）")
        t.add_argument("--focus", help="关注范围，逗号分隔")
        t.add_argument("--keywords", help="判定关键词，逗号分隔")
        t.add_argument("--language", choices=["zh", "en"])
        t.add_argument("--times", help="固定时点，如 08:30,21:00")
        t.add_argument("--interval", type=int, help="间隔分钟数")
        t.add_argument("--weekdays", help="运行星期，1=周一")
        t.add_argument("--enable", action="store_true")
        t.add_argument("--disable", action="store_true")
        t.set_defaults(func=cmd_topics)
    t_rm = ts.add_parser("rm", help="删除关注项（历史保留）")
    t_rm.add_argument("--id", required=True)
    t_rm.set_defaults(func=cmd_topics)

    s = sub.add_parser("config", help="查看/修改用户设置")
    cs = s.add_subparsers(dest="action", required=True)
    c_show = cs.add_parser("show", help="查看当前设置")
    c_show.set_defaults(func=cmd_config_show)
    c_set = cs.add_parser("set", help="修改设置并写回配置文件")
    c_set.add_argument("--role")
    c_set.add_argument("--focus", help="关注范围，逗号分隔")
    c_set.add_argument("--avoid", help="回避主题，逗号分隔")
    c_set.add_argument("--keywords", help="判定用关键词，逗号分隔")
    c_set.add_argument("--language", choices=["zh", "en"], help="输出语言")
    c_set.add_argument("--instructions", help="自定义要求（作为可信用户指令注入判定）")
    c_set.add_argument("--tokens-run", type=int, dest="tokens_run", help="单轮 token 上限")
    c_set.add_argument("--tokens-day", type=int, dest="tokens_day", help="每日 token 上限")
    c_set.add_argument("--on-exceed", choices=["degrade", "stop"], dest="on_exceed")
    c_set.add_argument("--mode", choices=["times", "interval"], help="调度模式")
    c_set.add_argument("--times", help="固定时点，如 08:30,21:00")
    c_set.add_argument("--interval", type=int, help="间隔分钟数（mode=interval）")
    c_set.add_argument("--weekdays", help="运行星期，逗号分隔，1=周一")
    c_set.add_argument("--active-hours", dest="active_hours", help="活跃时段，如 07:30-23:00")
    c_set.add_argument("--max-items", type=int, dest="max_items", help="每轮最多推送条数")
    c_set.add_argument("--channels", help="启用渠道，如 feishu,dingtalk")
    c_set.add_argument("--feishu-mode", choices=["webhook", "app"], dest="feishu_mode")
    c_set.add_argument("--feishu-receive-id", dest="feishu_receive_id")
    c_set.add_argument("--ai", choices=["on", "off"], help="AI 辅助总开关")
    c_set.add_argument("--triage", choices=["on", "off"], help="标题级初筛开关")
    c_set.add_argument("--silence", help="静默阈值：on/off，或 0.55 这样的置信下限")
    c_set.add_argument("--creator", choices=["on", "off"], help="选题与脚本大纲开关")
    c_set.add_argument("--briefing", choices=["on", "off"], help="晚间简报开关")
    c_set.add_argument("--briefing-time", dest="briefing_time", help="简报时间，如 21:30")
    c_set.add_argument("--feishu-tasks", dest="feishu_tasks", choices=["on", "off"],
                       help="推送时顺带建飞书待办（需 app 模式）")
    c_set.set_defaults(func=cmd_config_set)

    s = sub.add_parser("sources", help="采集源：查看 / 自动选源 / 单个 feed 体检")
    ss = s.add_subparsers(dest="action", required=True)
    ss.add_parser("list", help="列出已配置的源与上次自动选源结果").set_defaults(func=cmd_sources)
    au = ss.add_parser("auto", help="没有配置源时自动挑源（信任白名单 + 真抓校验）")
    au.add_argument("--topic", default="", help="按某个关注项挑源")
    au.add_argument("--apply", action="store_true", help="写进 config.sources.rss")
    au.add_argument("--refresh", action="store_true", help="忽略缓存，重新挑一次")
    au.set_defaults(func=cmd_sources)
    te = ss.add_parser("test", help="体检一个 feed URL：信任校验 + 抓取解析")
    te.add_argument("url")
    te.add_argument("--allow-unknown", dest="allow_unknown", action="store_true",
                    help="允许白名单外的域名（不建议）")
    te.set_defaults(func=cmd_sources)

    s = sub.add_parser("feedback", help="记录反馈；不带 --id 时列出最近条目")
    s.add_argument("--id")
    s.add_argument("--action", choices=["up", "down", "save", "open"])
    s.add_argument("--topic", default="", help="作用到哪个关注项；留空按条目归属")
    s.add_argument("--reason", default="",
                   help="为什么没用：" + " / ".join(feedback.codes()) + "（仅 --action down）")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_feedback)

    s = sub.add_parser("stats", help="查看权重、token 与最近运行")
    s.add_argument("--topic", default="", help="只看某个关注项的权重明细")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("trace", help="查看最近一次 trace 摘要")
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("feishu", help="飞书连通性调试")
    fs = s.add_subparsers(dest="action", required=True)
    fs.add_parser("chats", help="列出机器人所在群（app 模式）").set_defaults(func=cmd_feishu)
    fs.add_parser("test", help="发一条测试消息").set_defaults(func=cmd_feishu)

    s = sub.add_parser("dingtalk", help="钉钉连通性调试")
    s.add_argument("action", nargs="?", default="test", choices=["test"],
                   help="发一条测试消息")
    s.set_defaults(func=cmd_dingtalk)
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
