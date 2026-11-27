"""飞书待办写入：把「值得动手」的那一条变成一条真的待办。

为什么这一步值得单独做：推送解决「知道」，待办解决「会做」。一条高危依赖更新
如果不落到待办里，第三天还在原处。视频里让人觉得贴心的地方也在这——它把
「前置检查、后置验证」写进了任务描述，而不是只丢一个链接。

能力边界（必须写清楚，否则用户会以为是全自动）：
  * 只支持 app 模式（tenant_access_token）。webhook 自定义机器人没有任务 API 权限。
  * 需要应用开通 task:task:write；未开通时返回明确错误，不静默失败。
  * 只**建**待办，不回读完成状态——回读要事件订阅，属于另一套东西。
    所以这里也不做「未完成就继续催」的循环。
  * 幂等两层：本地 tasks_out 表 + 每个 URL 只建一次。重跑不会重复堆任务。

筛选条件放在配置里而不是代码里：actionability 低于阈值、confidence 不足、
超过单轮条数，都不建。宁可少建，也不要让待办列表变成第二个信息流。
"""

from __future__ import annotations

import json
from typing import Any

from . import feishu
from .models import Item
from .util import clip

DEFAULT_MIN_ACTIONABILITY = 7.0
DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_MAX_PER_RUN = 2

_STEPS_ZH = {
    "new_version": ["读 release notes，确认是否有 breaking change",
                    "在独立分支上升级并锁版本",
                    "跑测试与冒烟验证", "合并后再删旧版本兜底"],
    "new_release": ["确认与现有栈的兼容性", "在沙箱里跑一遍最小用例",
                    "记录回滚方式", "评估是否替换现有方案"],
    "controversy": ["确认自己是否在影响范围内", "查官方公告与修复版本",
                    "临时缓解（配置/降级）", "升级或打补丁后复验"],
    "funding": ["判断对开源策略/定价的影响", "确认许可证与路线图是否变化",
                "评估替代方案的必要性", "记录结论"],
    "benchmark": ["确认测试条件与硬件是否可比", "在本地复现关键数字",
                  "判断是否值得切换", "记录差异原因"],
}
_STEPS_EN = {
    "new_version": ["Read the release notes for breaking changes",
                    "Upgrade on a branch and pin the version",
                    "Run tests and a smoke check", "Merge, then drop the old fallback"],
    "new_release": ["Check compatibility with the current stack",
                    "Run a minimal case in a sandbox",
                    "Write down the rollback path", "Decide whether to adopt"],
    "controversy": ["Check whether you are in the blast radius",
                    "Find the official advisory and fixed version",
                    "Apply a temporary mitigation", "Verify after patching"],
    "funding": ["Judge the impact on licensing and pricing",
                "Check whether the roadmap changes",
                "Assess whether an alternative is needed", "Record the conclusion"],
    "benchmark": ["Confirm the benchmark setup is comparable",
                  "Reproduce the key numbers locally",
                  "Decide whether to switch", "Record why results differ"],
}
_GENERIC_ZH = ["读原文，确认关键事实与数字", "判断与当前项目的实际关系",
               "决定做什么（或明确不做）", "记录结论与后续观察点"]
_GENERIC_EN = ["Read the source and confirm the key facts",
               "Judge how it actually affects your project",
               "Decide what to do (or explicitly not to)",
               "Record the conclusion and what to watch next"]


def _section(cfg: dict[str, Any]) -> dict[str, Any]:
    return ((cfg.get("channels") or {}).get("feishu") or {}).get("tasks") or {}


def enabled(cfg: dict[str, Any]) -> bool:
    section = _section(cfg)
    feishu_cfg = (cfg.get("channels") or {}).get("feishu") or {}
    if not section.get("enabled"):
        return False
    if str(feishu_cfg.get("mode") or "webhook") != "app":
        return False
    return bool(feishu.configured(cfg))


def steps_for(item: Item, lang: str = "zh") -> list[str]:
    table = _STEPS_EN if lang == "en" else _STEPS_ZH
    generic = _GENERIC_EN if lang == "en" else _GENERIC_ZH
    return table.get(item.change_type or "", generic)


def build_description(item: Item, lang: str = "zh", precheck: str = "",
                      postcheck: str = "") -> str:
    zh = lang != "en"
    head = item.summary or item.raw_summary or item.title
    facts = "；".join(item.points[:3]) or clip(item.raw_summary or item.title, 120)
    lines = [
        clip(head, 300),
        "",
        ("要点：" if zh else "Key facts: ") + facts,
        ("原文：" if zh else "Source: ") + item.url,
        "",
        ("## 前置检查" if zh else "## Before"),
        clip(precheck or ("确认当前版本 / 影响范围，读一遍 release notes 与 issue"
                          if zh else
                          "Confirm the current version and blast radius; skim the notes"), 160),
        "",
        ("## 步骤" if zh else "## Steps"),
    ]
    for idx, step in enumerate(steps_for(item, lang), 1):
        lines.append(f"{idx}. {step}")
    lines += [
        "",
        ("## 后置验证" if zh else "## After"),
        clip(postcheck or ("跑测试或用例验证，确认问题消失、没有引入新问题"
                          if zh else
                          "Run tests / a minimal case to confirm the fix and no regressions"), 160),
    ]
    return "\n".join(lines)


def create(cfg: dict[str, Any], item: Item, lang: str = "zh",
           tracer: Any = None) -> dict[str, Any]:
    """建一条待办。返回 {ok, guid, detail}；失败不抛，由调用方决定是否影响推送。"""
    section = _section(cfg)
    due_hours = section.get("due_hours")
    payload: dict[str, Any] = {
        "summary": clip(item.title, 120),
        "description": build_description(item, lang),
        "origin": {"platform_i18n_name": "intel-agent"},
    }
    assignee = str(section.get("assignee_open_id") or "").strip()
    if assignee:
        payload["members"] = [{"id": assignee, "type": "user", "role": "assignee"}]
    tasklist = str(section.get("tasklist_guid") or "").strip()
    if tasklist:
        payload["tasklists"] = [{"tasklist_guid": tasklist}]
    if due_hours:
        import time as _time

        payload["due"] = {"timestamp": str(int((_time.time() + float(due_hours) * 3600) * 1000)),
                          "is_all_day": False}

    if section.get("dry_run"):
        if tracer:
            tracer.event("feishu_task.dry_run", item_id=item.id)
        return {"ok": True, "guid": "", "detail": "dry-run：未真正创建"}

    try:
        data = feishu._api(cfg, "/open-apis/task/v2/tasks", payload)
    except Exception as exc:  # 权限/字段错误都要如实回传，不假装成功
        if tracer:
            tracer.event("feishu_task.failed", item_id=item.id, error=str(exc)[:200])
        return {"ok": False, "guid": "", "detail": str(exc)[:200]}

    guid = str(((data.get("data") or {}).get("task") or {}).get("guid") or "")
    if tracer:
        tracer.event("feishu_task.created", item_id=item.id, guid=guid)
    return {"ok": True, "guid": guid, "detail": json.dumps({"guid": guid}, ensure_ascii=False)}


def eligible(items: list[Item], cfg: dict[str, Any]) -> list[Item]:
    section = _section(cfg)
    floor = float(section.get("min_actionability", DEFAULT_MIN_ACTIONABILITY))
    conf = float(section.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
    cap = int(section.get("max_per_run", DEFAULT_MAX_PER_RUN))
    picked = [it for it in items
              if float(it.gates.get("actionability", 0) or 0) >= floor
              and float(it.confidence or 0) >= conf]
    picked.sort(key=lambda it: (it.gates.get("actionability", 0), it.confidence), reverse=True)
    return picked[:cap]


def dispatch(cfg: dict[str, Any], items: list[Item], store: Any, lang: str = "zh",
             tracer: Any = None) -> list[dict[str, Any]]:
    """对符合条件的条目建待办。返回结果列表（供 CLI / 卡片回显）。"""
    if not enabled(cfg):
        return []
    out: list[dict[str, Any]] = []
    for item in eligible(items, cfg):
        if not item.canonical_url:
            continue
        if store.task_exists(item.canonical_url):
            out.append({"channel": "feishu_task", "ok": True, "detail": "已建过，跳过",
                        "item_id": item.id, "guid": "", "count": 0})
            continue
        res = create(cfg, item, lang, tracer=tracer)
        if res["ok"]:
            store.log_task(item.canonical_url, res["guid"], clip(item.title, 120))
        out.append({"channel": "feishu_task", "ok": res["ok"], "detail": res["detail"],
                    "item_id": item.id, "guid": res["guid"], "count": 1 if res["ok"] else 0})
    return out
