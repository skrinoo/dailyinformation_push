"""关注项（topic）：一路订阅 = 一套关注范围 + 指令 + 频率。

为什么不是「复制多份 config.json」：那样改一次渠道凭据要改 N 个文件，
而且水位线、推送幂等、token 账本会被拆成互不相干的多份，用户看到的是重复推送。

所以关注项只覆盖「会影响这一路收什么、什么时候收、怎么说话」的字段：
  profile.*     关注范围 / 附加要求（用户提示词）/ 输出语言
  filter.*      关键词、屏蔽词、每轮条数、闸门阈值
  sources.*     源与检索词（rss 列表整体替换，不做合并——合并会让删源失效）
  schedule.*    频率与时段（timezone 永远跟全局，避免一路在东八区一路在 UTC）
  output.creator 选题开关

它刻意**没有**状态字段：没有「完成/未完成」、没有「负责人」、没有进度。
关注项是订阅，不是任务；要待办时由收件侧（feishu_task）在推送时顺手建一条。

向后兼容：config 里没有 topics 时，等价于只有一个 id="" 的关注项，
行为与 v2 完全一致，老库、老水位线都不用迁移。
"""

from __future__ import annotations

import copy
import re
from typing import Any

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(text or "").strip().lower()).strip("-")
    return slug[:32] or "topic"


def raw_topics(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in (cfg.get("topics") or []) if isinstance(t, dict)]


def list_topics(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """返回补全后的关注项列表；没有 topics 时返回一个隐式的单关注项。"""
    topics = raw_topics(cfg)
    if not topics:
        return [{
            "id": "",
            "name": "默认",
            "enabled": True,
            "schedule": copy.deepcopy(cfg.get("schedule") or {}),
            "implicit": True,
        }]
    out: list[dict[str, Any]] = []
    for idx, topic in enumerate(topics):
        tid = str(topic.get("id") or "").strip().lower()
        if not ID_RE.match(tid):
            tid = _slug(topic.get("name") or f"topic{idx + 1}")
        row = copy.deepcopy(topic)
        row["id"] = tid
        row["name"] = str(topic.get("name") or tid or "默认")
        row["enabled"] = bool(topic.get("enabled", True))
        merged = copy.deepcopy(cfg.get("schedule") or {})
        merged.update({k: v for k, v in (topic.get("schedule") or {}).items()
                       if k in ("mode", "times", "interval_minutes", "active_hours", "weekdays")})
        row["schedule"] = merged
        out.append(row)
    return out


def get_topic(cfg: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    for topic in list_topics(cfg):
        if topic["id"] == (topic_id or ""):
            return topic
    return None


def topic_ids(cfg: dict[str, Any]) -> list[str]:
    return [t["id"] for t in list_topics(cfg)]


def enabled_ids(cfg: dict[str, Any]) -> list[str]:
    return [t["id"] for t in list_topics(cfg) if t.get("enabled", True)]


def _overrides(topic: dict[str, Any]) -> dict[str, Any]:
    """只挑白名单字段，防止关注项里写错一个键就把全局配置顶掉。"""
    out: dict[str, Any] = {}
    profile_keys = ("role", "interests", "avoid", "custom_instructions", "output_language")
    profile = {k: topic[k] for k in profile_keys if topic.get(k) is not None}
    if profile:
        out["profile"] = profile

    # silence / confidence 必须可覆盖：不同人格的「低于多少不推」「什么算即时」
    # 差别很大（入门者 0.5、投资人 0.7），写死全局等于让 12 个关注项共用一个阈值。
    filter_keys = ("keywords", "block_keywords", "max_items_per_run", "gates",
                   "silence", "confidence")
    filt = {k: topic[k] for k in filter_keys if topic.get(k) is not None}
    if filt:
        out["filter"] = filt

    if isinstance(topic.get("sources"), dict):
        out["sources"] = copy.deepcopy(topic["sources"])
    if isinstance(topic.get("output"), dict):
        out["output"] = copy.deepcopy(topic["output"])
    return out


def effective(cfg: dict[str, Any], topic_id: str = "") -> dict[str, Any]:
    """把关注项的覆盖合并进全局配置，返回一份可以直接跑 pipeline 的配置副本。

    合并规则：字典逐层合并；列表整体替换（删一个 RSS 源必须真的删掉）。
    """
    topic = get_topic(cfg, topic_id)
    if topic is None:
        raise KeyError(f"未知关注项：{topic_id}")
    merged = copy.deepcopy(cfg)
    if topic.get("implicit"):
        merged["topic_id"] = ""
        return merged

    for section, values in _overrides(topic).items():
        base = merged.get(section)
        if isinstance(base, dict) and isinstance(values, dict):
            for key, value in values.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    base[key] = {**base[key], **value}
                else:
                    base[key] = copy.deepcopy(value)
        else:
            merged[section] = copy.deepcopy(values)

    merged["topic_id"] = topic["id"]
    merged["topic_name"] = topic["name"]
    return merged


def _width(text: str) -> int:
    """按显示宽度算（CJK 占 2 列）。用 len() 补空格会让中文列永远差半格。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _width(text))


def describe(topic: dict[str, Any]) -> str:
    from .scheduler import describe as describe_schedule

    flag = "" if topic.get("enabled", True) else "（已停用）"
    return f"{_pad(topic['id'] or 'default', 16)}{_pad(topic.get('name', ''), 16)}" \
           f"{describe_schedule(topic.get('schedule') or {})}{flag}"
