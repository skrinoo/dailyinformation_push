"""配置装载与校验。

设计原则：所有判定阈值都在这里，不在 prompt 里。用户抱怨「推太多」时改配置，
不必改模型行为——这是可运维性与可复现性的前提。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "profile": {
        "role": "",
        # 关注范围：用户自己填，AI 的检索规划与判定都以它为准
        "interests": [],
        "avoid": [],
        # 自由文本要求，会作为「可信的用户指令」注入判定 prompt
        "custom_instructions": "",
        # 最终呈现语言：zh | en
        "output_language": "zh",
    },
    # Token 预算：用户按「每天愿意让它读多少」设，不按钱设
    "budget": {
        "tokens_per_run": 60000,
        "tokens_per_day": 200000,
        "on_exceed": "degrade",
        "output_reserve_tokens": 900,
    },
    # 关注项：每一路独立配置。留空 = 单关注项模式（行为与 v2 一致）
    "topics": [],
    # 采集阶段的 AI 辅助（规划检索 + 标题级初筛）
    "ai": {
        "enabled": True,
        "plan_queries": True,
        "query_ttl_hours": 24,
        "max_queries": 6,
        "triage": True,
        "triage_batch_size": 40,
    },
    "sources": {
        "github": {
            "enabled": True,
            "topics": ["llm-agent"],
            "languages": [],
            "since_days": 14,
            "min_stars": 80,
            "limit": 24,
            "token_env": "GITHUB_TOKEN",
        },
        "hn": {
            "enabled": True,
            "queries": [],
            "min_points": 20,
            "since_hours": 48,
            "limit": 15,
        },
        "rss": [],
        "page": {
            "enabled": False,
            "targets": [],
            "max_bytes": 2_000_000,
        },
        # 兜底选源：只有四类源全空时才触发，见 autosource.needs_discovery
        "auto": {
            "enabled": True,
            "max_feeds": 6,
            "ttl_hours": 168,
            "allow_unknown": False,
            "validate": True,
            "timeout": 15,
        },
    },
    "filter": {
        "keywords": [],
        "block_keywords": [],
        "max_items_per_run": 8,
        # 四闸：每维 0-10，低于 floor 直接淘汰，不参与加权
        "gates": {
            "relevance": {"floor": 4.0, "weight": 0.45},
            "importance": {"floor": 3.0, "weight": 0.25},
            "novelty": {"floor": 3.0, "weight": 0.15},
            "actionability": {"floor": 3.0, "weight": 0.15},
        },
        # 首次运行没有历史可比，novelty 闸门放宽
        "first_run_floor_discount": 1.5,
        "coarse_keep_ratio": 0.28,
        "coarse_min_keep": 12,
        "max_candidates_for_llm": 40,
        "source_quota_ratio": 0.6,
        # 粗排阶段给每个来源保底一条：召回层不能把某个语言整体清零
        "coarse_source_floor": True,
        # 中英文配比：只在已通过四闸的候选之间换位，不用不达标内容凑数
        "language_mix": {"enabled": True, "zh_ratio": 0.34},
        # 渠道分流的置信度切点
        "confidence": {"immediate": 0.80, "daily": 0.50, "archive_below": 0.35},
        # 静默：本轮没有任何条目达到这个置信度就不打扰，内容留到晚间简报
        "silence": {"enabled": True, "min_confidence": 0.55},
    },
    # 收件侧加工：选题与脚本大纲（内容创作向）
    "output": {
        "creator": {"enabled": True, "min_score": 5.0, "max_candidates": 6},
    },
    "cluster": {
        "enabled": True,
        "window_hours": 72,
        "max_members": 12,
    },
    "dedupe": {
        "title_threshold": 0.80,
        "length_ratio_max": 0.30,
        "short_title_words": 5,
        "window_hours": 24,
        "keep": "earliest",
    },
    "watermark": {
        "enabled": True,
        "overlap_hours": 6,
        "max_backfill_hours": 168,
    },
    "llm": {
        "enabled": False,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "LLM_API_KEY",
        "temperature": 0.0,
        "timeout": 90,
        "max_retries": 2,
        "max_input_chars": 1800,
        "max_output_tokens": 1200,
        # 价格可选：填了才在报表里折算成钱，预算本身只认 token
        "price_per_1k_in": 0,
        "price_per_1k_out": 0,
    },
    "channels": {
        "feishu": {
            "enabled": False,
            # webhook = 自定义机器人；app = 企业自建应用（可主动发消息、可列群）
            "mode": "webhook",
            "webhook_env": "FEISHU_WEBHOOK",
            "secret_env": "FEISHU_SECRET",
            "app_id_env": "FEISHU_APP_ID",
            "app_secret_env": "FEISHU_APP_SECRET",
            "receive_id": "",
            "receive_id_type": "chat_id",
            "api_base": "https://open.feishu.cn",
            # 待办写入：只建不回读。webhook 机器人没有任务 API 权限，必须 app 模式
            "tasks": {
                "enabled": False,
                "min_actionability": 7.0,
                "min_confidence": 0.6,
                "max_per_run": 2,
                "tasklist_guid": "",
                "assignee_open_id": "",
                "due_hours": 0,
                "dry_run": False,
            },
        },
        "dingtalk": {
            "enabled": False,
            # 群机器人 webhook 自带 access_token；secret 只在开了「加签」时才需要
            "webhook_env": "DINGTALK_WEBHOOK",
            "secret_env": "DINGTALK_SECRET",
            "api_base": "https://oapi.dingtalk.com",
        },
        "telegram": {
            "enabled": False,
            "bot_token_env": "TELEGRAM_BOT_TOKEN",
            "chat_id_env": "TELEGRAM_CHAT_ID",
        },
        "heartbeat": {"enabled": True, "after_empty_runs": 2},
    },
    "schedule": {
        # times = 每天固定时点；interval = 活跃时段内每 N 分钟
        "mode": "times",
        "times": ["08:30"],
        "interval_minutes": 120,
        "active_hours": ["07:30", "23:00"],
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
        "timezone": "Asia/Shanghai",
        # 晚间简报：一天结束时兜底，静默的那几轮也在这里交代
        "briefing": {"enabled": False, "time": "21:30"},
    },
    "storage": {
        "db": "data/intel.db",
        "out_dir": "out",
        "trace_dir": "traces",
    },
    "observability": {
        "trace_enabled": True,
        "log_item_decisions": True,
        "pii_redact": True,
    },
    "runtime": {
        "http_timeout": 25,
        "http_retries": 3,
        "retry_backoff_base": 0.8,
        "retry_jitter": 0.3,
        "source_deadline_seconds": 90,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def validate(cfg: dict[str, Any]) -> list[str]:
    """返回错误列表。空列表表示通过。宁可启动失败，也不要带病跑一整晚。"""
    errors: list[str] = []
    gates = cfg["filter"].get("gates") or {}
    if set(gates) != {"relevance", "importance", "novelty", "actionability"}:
        errors.append("filter.gates 必须恰好包含 relevance/importance/novelty/actionability 四维")
    total = sum(float(g.get("weight", 0)) for g in gates.values())
    if abs(total - 1.0) > 0.02:
        errors.append(f"filter.gates 权重之和应为 1.0，当前 {total:.3f}")
    conf = cfg["filter"].get("confidence") or {}
    if not (0 < float(conf.get("daily", 0)) < float(conf.get("immediate", 0)) <= 1):
        errors.append("filter.confidence 必须满足 0 < daily < immediate <= 1")
    if int(cfg["filter"].get("max_items_per_run", 0)) <= 0:
        errors.append("filter.max_items_per_run 必须大于 0")
    quota = float(cfg["filter"].get("source_quota_ratio", 0))
    if not 0 < quota <= 1:
        errors.append("filter.source_quota_ratio 必须在 (0,1]")
    if cfg["llm"].get("enabled") and not cfg["llm"].get("model"):
        errors.append("llm.enabled=true 时必须配置 llm.model")

    # ---- 兜底选源 ----
    auto = (cfg.get("sources") or {}).get("auto") or {}
    if auto:
        try:
            if not 1 <= int(auto.get("max_feeds") or 6) <= 12:
                errors.append("sources.auto.max_feeds 应在 1-12 之间")
            if not 1 <= int(auto.get("ttl_hours") or 168) <= 720:
                errors.append("sources.auto.ttl_hours 应在 1-720 之间")
        except (TypeError, ValueError):
            errors.append("sources.auto.max_feeds / ttl_hours 必须是数字")

    # ---- 输出语言 ----
    lang = str((cfg.get("profile") or {}).get("output_language") or "zh").lower()
    if lang not in ("zh", "en", "cn", "chinese", "中文"):
        errors.append("profile.output_language 只能是 zh 或 en")

    # ---- Token 预算 ----
    budget = cfg.get("budget") or {}
    for key in ("tokens_per_run", "tokens_per_day"):
        try:
            if int(budget.get(key) or 0) < 0:
                errors.append(f"budget.{key} 不能为负数")
        except (TypeError, ValueError):
            errors.append(f"budget.{key} 必须是整数 token 数")
    if str(budget.get("on_exceed") or "degrade") not in ("degrade", "stop"):
        errors.append("budget.on_exceed 只能是 degrade 或 stop")

    # ---- AI 辅助 ----
    ai = cfg.get("ai") or {}
    try:
        if not 1 <= int(ai.get("max_queries") or 6) <= 12:
            errors.append("ai.max_queries 必须在 1-12 之间")
        if int(ai.get("triage_batch_size") or 40) < 10:
            errors.append("ai.triage_batch_size 至少为 10")
    except (TypeError, ValueError):
        errors.append("ai.max_queries / triage_batch_size 必须是整数")

    # ---- 调度 ----
    from .scheduler import MIN_INTERVAL_MINUTES, parse_hhmm

    sched = cfg.get("schedule") or {}
    if str(sched.get("mode") or "times") not in ("times", "interval"):
        errors.append("schedule.mode 只能是 times 或 interval")
    for value in sched.get("times") or []:
        if not parse_hhmm(value):
            errors.append(f"schedule.times 中的 {value!r} 不是合法的 HH:MM")
    hours = sched.get("active_hours") or []
    if hours and (len(hours) != 2 or not all(parse_hhmm(h) for h in hours)):
        errors.append("schedule.active_hours 必须是 [开始, 结束] 两个 HH:MM")
    try:
        if sched.get("mode") == "interval" and int(sched.get("interval_minutes") or 0) < MIN_INTERVAL_MINUTES:
            errors.append(f"schedule.interval_minutes 不能小于 {MIN_INTERVAL_MINUTES}")
    except (TypeError, ValueError):
        errors.append("schedule.interval_minutes 必须是整数")
    for day in sched.get("weekdays") or []:
        try:
            if not 1 <= int(day) <= 7:
                errors.append("schedule.weekdays 取值必须在 1-7（1=周一）")
                break
        except (TypeError, ValueError):
            errors.append("schedule.weekdays 必须是整数列表")
            break

    # ---- 飞书 ----
    feishu = (cfg.get("channels") or {}).get("feishu") or {}
    mode = str(feishu.get("mode") or "webhook")
    if mode not in ("webhook", "app"):
        errors.append("channels.feishu.mode 只能是 webhook 或 app")
    if feishu.get("enabled") and mode == "app" and not feishu.get("receive_id"):
        errors.append("飞书 app 模式需要配置 channels.feishu.receive_id"
                      "（先用 python main.py feishu chats 查 chat_id）")

    # ---- 钉钉 ----
    dingtalk = (cfg.get("channels") or {}).get("dingtalk") or {}
    if dingtalk.get("enabled") and not str(dingtalk.get("webhook_env") or "").strip():
        errors.append("钉钉启用后需要填 channels.dingtalk.webhook_env"
                      "（存放群机器人 webhook 地址的环境变量名）")

    # ---- 关注项 ----
    from .topics import ID_RE

    seen_ids: set[str] = set()
    for idx, topic in enumerate(cfg.get("topics") or []):
        if not isinstance(topic, dict):
            errors.append(f"topics[{idx}] 必须是对象")
            continue
        tid = str(topic.get("id") or "").strip().lower()
        if not tid:
            errors.append(f"topics[{idx}] 缺少 id（用于水位线与历史分区）")
            continue
        if not ID_RE.match(tid):
            errors.append(f"topics[{idx}].id 只能用小写字母/数字/连字符：{tid!r}")
        if tid in seen_ids:
            errors.append(f"topics 里 id 重复：{tid}")
        seen_ids.add(tid)
        sub = topic.get("schedule") or {}
        if sub:
            for value in sub.get("times") or []:
                if not parse_hhmm(value):
                    errors.append(f"topics[{idx}].schedule.times 中的 {value!r} 不是合法 HH:MM")
            if str(sub.get("mode") or "") and str(sub.get("mode")) not in ("times", "interval"):
                errors.append(f"topics[{idx}].schedule.mode 只能是 times 或 interval")
            try:
                if sub.get("mode") == "interval" and int(sub.get("interval_minutes") or 0) < MIN_INTERVAL_MINUTES:
                    errors.append(f"topics[{idx}].schedule.interval_minutes 不能小于 {MIN_INTERVAL_MINUTES}")
            except (TypeError, ValueError):
                errors.append(f"topics[{idx}].schedule.interval_minutes 必须是整数")
        lang = str(topic.get("output_language") or "").lower()
        if lang and lang not in ("zh", "en", "cn", "chinese", "中文"):
            errors.append(f"topics[{idx}].output_language 只能是 zh 或 en")

    # ---- 静默与选题 ----
    silence = (cfg.get("filter") or {}).get("silence") or {}
    try:
        conf = float(silence.get("min_confidence", 0.55))
        if not 0 <= conf <= 1:
            errors.append("filter.silence.min_confidence 必须在 0-1 之间")
    except (TypeError, ValueError):
        errors.append("filter.silence.min_confidence 必须是 0-1 的数字")
    creator = ((cfg.get("output") or {}).get("creator") or {})
    try:
        if not 1 <= int(creator.get("max_candidates") or 6) <= 20:
            errors.append("output.creator.max_candidates 必须在 1-20 之间")
    except (TypeError, ValueError):
        errors.append("output.creator.max_candidates 必须是整数")

    # ---- 晚间简报 ----
    brief = sched.get("briefing") or {}
    if brief.get("enabled") and not parse_hhmm(str(brief.get("time") or "")):
        errors.append("schedule.briefing.time 必须是合法的 HH:MM")

    # ---- 飞书待办 ----
    tasks_cfg = feishu.get("tasks") or {}
    if tasks_cfg.get("enabled") and mode != "app":
        errors.append("飞书待办需要 channels.feishu.mode=app（webhook 机器人没有任务 API 权限）")
    return errors


def load_config(path: str | Path = "config.json") -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在：{p.resolve()}（先执行 python main.py init）")
    raw = json.loads(p.read_text(encoding="utf-8"))
    cfg = _deep_merge(DEFAULTS, raw)
    errors = validate(cfg)
    if errors:
        raise ValueError("配置校验失败：\n  - " + "\n  - ".join(errors))
    return cfg


def write_default(path: str | Path = "config.json", force: bool = False) -> Path:
    p = Path(path)
    if p.exists() and not force:
        return p
    p.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def backup_path(path: str | Path, stamp: str = "") -> Path:
    """配置备份统一放在 `data/backups/`，不再散落在项目根目录。

    stamp 为空时写固定名（最后一次，覆盖）；带 stamp 按份保留，供控制台滚动清理。
    """
    p = Path(path)
    target = p.parent / "data" / "backups"
    target.mkdir(parents=True, exist_ok=True)
    name = f"{p.name}.bak" if not stamp else f"{p.name}.bak-{stamp}"
    return target / name
