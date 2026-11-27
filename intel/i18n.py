"""输出语言（i18n）。

只覆盖「我们自己写的字」：标题、字段名、淘汰原因、心跳文案、兜底理由模板。
正文内容（摘要）由模型按 output.language 生成；规则模式下只能保留原文语言，
这一点在文档里明确写了，不做机器翻译假装是原意。
"""

from __future__ import annotations

from typing import Any

SUPPORTED = ("zh", "en")

_ZH = {
    "lang_name": "简体中文",
    "digest_title": "情报日报",
    "source": "来源",
    "verdict": "判定",
    "gates": "四闸",
    "also_reported": "另有 {n} 家报道",
    "original": "原文",
    "rejected_title": "淘汰明细（用于校准阈值）",
    "col_title": "标题",
    "col_reason": "淘汰原因",
    "col_score": "分数",
    "empty": "本轮没有达到阈值的内容。",
    "head_meta": "{date} · 入选 {kept} 条 · 淘汰 {rejected} 条",
    "count": "{n} 条",
    "conf": "置信",
    "heartbeat_title": "今日无重要更新",
    "heartbeat": "扫描 {scanned} 条，没有达到阈值的内容{tail}。当前相关性下限 {floor:.1f}，觉得太安静可以调低。",
    "heartbeat_reason": "本轮 0 条达到阈值",
    "skipped_empty": "本轮无新增条目，跳过发送",
    "note_footer": "{date} · {n} 条",
    "feedback_console": "觉得没用？到控制台勾选原因：{url}",
    "daily": "日报",
    "weekly": "周报",
    "gate_low": "{name} {value} 低于下限 {floor}",
    "blocked": "命中屏蔽词「{word}」",
    "quota": "单源配额已满",
    "evergreen": "常青项目，无新闻信号",
    "no_gates": "未产出判定",
    "coarse_cut": "粗排未进入候选",
    "folded": "已折叠进其他事件",
    "ai_triage": "采集层 AI 初筛：{why}",
    "changes": {
        "first_seen": "首次出现",
        "new_version": "新版本",
        "new_release": "新发布",
        "funding": "融资",
        "benchmark": "新基准",
        "controversy": "争议",
        "update": "有更新",
        "none": "",
    },
    "gates_label": {
        "relevance": "相关性",
        "importance": "重要性",
        "novelty": "新颖性",
        "actionability": "可行动性",
    },
    "no_change": "与上次相比没有可写的变化",
    "silenced": "本轮静默：最高置信 {conf} 未达打扰阈值 {floor}",
    "silent_note": "已静默 {n} 轮，内容留在晚间简报里",
    "pack_title": "今日选题",
    "pack_angle": "切入角度",
    "pack_basis": "依据",
    "pack_outline": "脚本大纲",
    "pack_refs": "参考资料",
    "pack_rule": "（规则生成，未调用模型）",
    "pack_titles": "标题备选",
    "task_created": "已写入飞书待办",
    "task_failed": "待办写入失败",
    "task_skip": "已达待办上限或已建过",
    "briefing_title": "晚间简报",
    "brief_meta": "{date} · 今日 {runs} 轮 · 扫描 {fetched} 条 · 入选 {kept} 条 · 推送 {pushed} 条",
    "brief_quiet": "其中 {n} 轮没有值得打扰的内容，已静默",
    "brief_topics": "按关注项",
    "brief_tasks": "今日新建待办 {n} 条",
    "brief_tasks_hint": "在飞书任务里勾掉后，它不会再提醒",
    "brief_empty": "今天没有值得收录的内容。沉默也是结论：阈值没有失控。",
    "brief_next": "明天继续盯：{topics}",
    "brief_topic_line": "{name} · {kept} 条",
    "brief_rejected": "今日淘汰 {n} 条（用于校准阈值）",
}

_EN = {
    "lang_name": "English",
    "digest_title": "Intel Digest",
    "source": "Source",
    "verdict": "Verdict",
    "gates": "Gates",
    "also_reported": "{n} other source(s)",
    "original": "Original",
    "rejected_title": "Rejected (for threshold tuning)",
    "col_title": "Title",
    "col_reason": "Reason",
    "col_score": "Score",
    "empty": "Nothing cleared the bar this run.",
    "head_meta": "{date} · {kept} selected · {rejected} rejected",
    "count": "{n} item(s)",
    "conf": "conf",
    "heartbeat_title": "No major updates",
    "heartbeat": "Scanned {scanned} items, none cleared the bar{tail}. Relevance floor is {floor:.1f}; lower it if this feels too quiet.",
    "heartbeat_reason": "0 items above threshold",
    "skipped_empty": "nothing new this run, skipped",
    "note_footer": "{date} · {n} item(s)",
    "feedback_console": "Not useful? Pick a reason in the console: {url}",
    "daily": "daily",
    "weekly": "weekly",
    "gate_low": "{name} {value} below floor {floor}",
    "blocked": "blocked keyword: {word}",
    "quota": "per-source quota reached",
    "evergreen": "evergreen project, no news signal",
    "no_gates": "no scores produced",
    "coarse_cut": "cut at coarse ranking",
    "folded": "folded into another event",
    "ai_triage": "AI triage at collection: {why}",
    "changes": {
        "first_seen": "new",
        "new_version": "new version",
        "new_release": "released",
        "funding": "funding",
        "benchmark": "new benchmark",
        "controversy": "controversy",
        "update": "updated",
        "none": "",
    },
    "gates_label": {
        "relevance": "relevance",
        "importance": "importance",
        "novelty": "novelty",
        "actionability": "actionability",
    },
    "no_change": "no change worth reporting since last time",
    "silenced": "Silenced: top confidence {conf} is below the {floor} bar",
    "silent_note": "{n} quiet run(s) folded into the evening brief",
    "pack_title": "Today's pick",
    "pack_angle": "Angle",
    "pack_basis": "Basis",
    "pack_outline": "Outline",
    "pack_refs": "References",
    "pack_rule": "(rule-generated, no model call)",
    "pack_titles": "Title options",
    "task_created": "Added to Feishu tasks",
    "task_failed": "Task creation failed",
    "task_skip": "cap reached or already created",
    "briefing_title": "Evening Brief",
    "brief_meta": "{date} · {runs} run(s) · {fetched} scanned · {kept} kept · {pushed} pushed",
    "brief_quiet": "{n} run(s) stayed silent — nothing worth interrupting you",
    "brief_topics": "By topic",
    "brief_tasks": "{n} task(s) created today",
    "brief_tasks_hint": "Tick them off in Feishu and it stops reminding you",
    "brief_empty": "Nothing worth keeping today. Silence is also an answer: the bar held.",
    "brief_next": "Still watching tomorrow: {topics}",
    "brief_topic_line": "{name} · {kept}",
    "brief_rejected": "{n} rejected today (for threshold tuning)",
}

_TABLE = {"zh": _ZH, "en": _EN}


def resolve_lang(cfg: dict[str, Any] | None) -> str:
    """配置里的 output_language，非法值回落到 zh，不抛错。"""
    raw = str(((cfg or {}).get("profile") or {}).get("output_language") or "zh").lower()
    if raw.startswith("en"):
        return "en"
    return "zh" if raw in ("zh", "cn", "chinese", "中文") else "zh"


def t(key: str, lang: str = "zh", **kwargs: Any) -> str:
    table = _TABLE.get(lang) or _ZH
    value = table.get(key)
    if value is None:
        value = _ZH.get(key, key)
    if isinstance(value, dict):
        return str(value)
    try:
        return str(value).format(**kwargs) if kwargs else str(value)
    except (KeyError, IndexError, ValueError):
        return str(value)


def labels(key: str, lang: str) -> dict[str, str]:
    """取字典型词表（changes / gates_label）。t() 只处理字符串。"""
    table = _TABLE.get(lang) or _ZH
    value = table.get(key) or _ZH.get(key) or {}
    return {k: str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def change_label(change_type: str, lang: str) -> str:
    return labels("changes", lang).get(change_type, "")


def gate_label(name: str, lang: str) -> str:
    return labels("gates_label", lang).get(name, name)
