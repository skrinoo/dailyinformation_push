"""四项收件侧能力的回归：关注项分区、飞书待办、选题大纲、静默与晚间简报。

这四项的共同风险是「看起来配了，实际没接上」——配置里多一个键很容易，
接进链路上很难。所以每一条都断言到可观测结果：
  * 分区的证据是「A 项跑过之后 B 项还能收到同一批条目」
  * 待办的证据是「本地幂等表里有记录，且第二次 dispatch 跳过」
  * 选题的证据是「模型编造的 URL 被丢掉、编造的 item_id 退回规则路径」
  * 静默的证据是「push 通道是 silenced 而不是 feishu」

用法：python tests/test_delivery_features.py
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import briefing, feishu_task, studio      # noqa: E402
from intel.config import load_config, validate      # noqa: E402
from intel.models import Item                       # noqa: E402
from intel.pipeline import run_once                 # noqa: E402
from intel.store import Store                       # noqa: E402
from intel.topics import effective, get_topic, list_topics  # noqa: E402

STUDIO_REPLIES: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        system = " ".join(m.get("content", "") for m in body.get("messages", [])
                          if m.get("role") == "system")
        user = " ".join(m.get("content", "") for m in body.get("messages", [])
                        if m.get("role") == "user")
        if "内容策划" in system:
            ids = re.findall(r'<candidate id="([^"]+)"', user)
            urls = re.findall(r"url: (\S+)", user)
            reply = STUDIO_REPLIES.pop(0) if STUDIO_REPLIES else {}
            if reply.get("use_first_id"):
                reply["item_id"] = ids[0] if ids else ""
            if reply.get("use_first_url"):
                reply["references"] = [{"title": "真实链接", "url": urls[0] if urls else ""},
                                       {"title": "编造链接", "url": "https://made-up.example/x"}]
            content = reply
        else:
            content = {"results": []}
        resp = json.dumps({"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
                           "usage": {"prompt_tokens": 500, "completion_tokens": 200}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _item(idx: int, actionability: float = 8.0, confidence: float = 0.8) -> Item:
    url = f"https://example.com/p/{idx}"
    item = Item(id=f"id{idx}", title=f"依赖升级 {idx}", url=url, canonical_url=url,
                source="github", published_at=datetime.now().isoformat(timespec="seconds"),
                summary="compression 4.1.0 修复了一个高危漏洞。", reason="命中安全补丁")
    item.gates = {"relevance": 8.0, "importance": 7.0, "novelty": 7.0,
                  "actionability": actionability}
    item.score = 7.5
    item.confidence = confidence
    item.verdict = "selected"
    item.change_type = "new_version"
    return item


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    os.environ["LLM_API_KEY"] = "test-key"

    tmp = Path(tempfile.mkdtemp())
    base = load_config(ROOT / "config.json")
    base["storage"] = {"db": str(tmp / "t.db"), "out_dir": str(tmp / "out"),
                       "trace_dir": str(tmp / "tr")}
    base["llm"].update({"enabled": True, "base_url": f"http://127.0.0.1:{port}/v1",
                        "model": "mock", "timeout": 10, "max_retries": 0})
    base["budget"] = {"tokens_per_run": 60000, "tokens_per_day": 200000,
                      "on_exceed": "degrade", "output_reserve_tokens": 200}

    # ================= 1. 关注项 =================
    topics = list_topics(base)
    check(len(topics) == 2, f"关注项数量异常：{len(topics)}")
    sec = effective(base, "sec-rev")
    check(sec["profile"]["output_language"] == "zh" and sec["topic_id"] == "sec-rev",
          "关注项覆盖未生效")
    check("cve" in [k.lower() for k in sec["filter"]["keywords"]], "关注项关键词未覆盖全局")
    check(get_topic(base, "nope") is None, "未知关注项未返回 None")
    check(list_topics(base)[1]["schedule"]["times"] == ["20:30"], "关注项独立频率未生效")

    # 分区的关键证据：A 项跑完，B 项仍能收到同一批条目
    store = Store(base["storage"]["db"])
    r_a = run_once(base, store, topic="ai-eng", dry_run=True, offline=True,
                   force_rule=True, verbose=False)
    r_b = run_once(base, store, topic="sec-rev", dry_run=True, offline=True,
                   force_rule=True, verbose=False)
    r_a2 = run_once(base, store, topic="ai-eng", dry_run=True, offline=True,
                    force_rule=True, verbose=False)
    check(r_b.get("after_watermark", 0) > 0,
          f"关注项 B 被 A 的水位/已知 URL 截胡：after_watermark={r_b.get('after_watermark')}")
    check(r_a2.get("after_watermark", 0) == 0,
          f"同一关注项重跑未幂等：after_watermark={r_a2.get('after_watermark')}")
    rows = store.topic_runs("ai-eng", limit=5) + store.topic_runs("sec-rev", limit=5)
    check(len(rows) >= 3, "运行记录没有按关注项落库")

    # ================= 2. 静默 =================
    loud = copy.deepcopy(base)
    loud["topics"] = []
    loud["filter"]["silence"] = {"enabled": True, "min_confidence": 0.0}
    quiet = copy.deepcopy(base)
    quiet["topics"] = []
    quiet["filter"]["silence"] = {"enabled": True, "min_confidence": 1.0}
    loud_store = Store(tmp / "loud.db")
    quiet_store = Store(tmp / "quiet.db")
    loud_res = run_once(loud, loud_store, dry_run=True, offline=True, force_rule=True, verbose=False)
    quiet_res = run_once(quiet, quiet_store, dry_run=True, offline=True, force_rule=True,
                         verbose=False)
    check(not loud_res.get("silenced"), "阈值 0 时不应静默")
    check(quiet_res.get("silenced") is True, "阈值 1 时应静默")
    check(quiet_res["push"][0]["channel"] == "silenced", "静默时 push 通道不对")
    check(quiet_res.get("kept", 0) > 0 and quiet_res["pack"] is None,
          "静默时不该继续花 token 做选题")

    # ================= 3. 选题与大纲 =================
    items = [_item(i) for i in range(3)]
    rule_pack = studio.build_pack(items, {**base, "llm": {**base["llm"], "enabled": False}})
    check(rule_pack and rule_pack["source"] == "rule", "无模型时未走规则选题")
    check(len(rule_pack["outline"]) >= 3, "规则大纲段落不足")
    check(rule_pack["references"][0]["url"] in {it.url for it in items},
          "规则大纲的参考链接不在候选内")

    STUDIO_REPLIES.append({
        "use_first_id": True, "use_first_url": True,
        "title_options": ["标题一", "标题二"],
        "angle": "别人讲版本号，我讲影响面",
        "why_now": "补丁刚发布 6 小时",
        "outline": [{"section": "事实", "points": ["4.1.0 修复 CVE-2026-1", "影响 <=4.0.9"]},
                    {"section": "验证", "points": ["跑 npm i 后复验"]}],
    })
    llm_pack = studio.build_pack(items, base, api_key="test-key")
    check(llm_pack and llm_pack["source"] == "llm", "模型路径未生效")
    refs = {r["url"] for r in llm_pack["references"]}
    check("https://made-up.example/x" not in refs, "模型编造的参考链接未被丢弃")
    check(len(refs) == 1, f"参考链接数量异常：{refs}")

    STUDIO_REPLIES.append({"item_id": "不存在的id", "outline": [{"section": "x", "points": ["y"]}]})
    bad_pack = studio.build_pack(items, base, api_key="test-key")
    check(bad_pack and bad_pack["source"] == "rule", "非法 item_id 未退回规则路径")

    # ================= 4. 飞书待办 =================
    task_cfg = copy.deepcopy(base)
    task_cfg["topics"] = []
    task_cfg["channels"]["feishu"].update({"mode": "app", "enabled": True, "receive_id": "oc_x"})
    task_cfg["channels"]["feishu"]["tasks"] = {
        "enabled": True, "min_actionability": 7.0, "min_confidence": 0.6,
        "max_per_run": 2, "tasklist_guid": "", "assignee_open_id": "", "due_hours": 0,
        "dry_run": True,
    }
    feishu_task.feishu.configured = lambda cfg: True  # 只测链路，不测凭据
    ok_items = [_item(10, 9.0, 0.9), _item(11, 8.0, 0.8), _item(12, 5.0, 0.9)]
    picked = feishu_task.eligible(ok_items, task_cfg)
    check(len(picked) == 2, f"待办筛选/上限错误：{len(picked)}")
    desc = feishu_task.build_description(picked[0], "zh")
    check("前置检查" in desc and "后置验证" in desc and picked[0].url in desc,
          "待办描述缺少前后置检查或原文")
    res_first = feishu_task.dispatch(task_cfg, ok_items, store, "zh")
    check(len(res_first) == 2 and all(r["ok"] for r in res_first), f"待办写入失败：{res_first}")
    res_second = feishu_task.dispatch(task_cfg, ok_items, store, "zh")
    check(all(r["detail"].startswith("已建过") for r in res_second), "待办幂等未生效")
    check(store.task_exists(picked[0].canonical_url), "待办没有落幂等表")

    errs = validate({**task_cfg, "channels": {**task_cfg["channels"], "feishu": {
        **task_cfg["channels"]["feishu"], "mode": "webhook"}}})
    check(any("app" in e for e in errs), "webhook 模式下启用待办未被拦截")

    # ================= 5. 晚间简报 =================
    summary = briefing.collect(store, base, day="today")
    check(summary["runs"] >= 3, f"简报轮次统计错误：{summary['runs']}")
    check(summary["kept"] >= 1, "简报入选统计为 0")
    names = {tp["id"] for tp in summary["topics"]}
    check("ai-eng" in names or "sec-rev" in names, f"简报没按关注项分组：{names}")
    brief_res = briefing.run_briefing(base, store, day="today", dry_run=True, verbose=False)
    html = Path(brief_res["html"]).read_text(encoding="utf-8")
    check("晚间简报" in html and "按关注项" in html, "简报 HTML 内容不完整")
    check("ai-eng" in html or "AI 工程" in html, "简报 HTML 未包含关注项")
    check(len(summary["tasks"]) >= 2, f"简报待办统计错误：{len(summary['tasks'])}")

    srv.shutdown()
    store.close()
    loud_store.close()
    quiet_store.close()

    if failures:
        for f in failures:
            print("  FAIL", f)
        print(f"收件侧能力检查：FAIL（{len(failures)} 项）")
        return 1
    print(f"收件侧能力检查：PASS（简报 {summary['runs']} 轮 / {summary['kept']} 条 / "
          f"{len(summary['tasks'])} 条待办）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
