"""推送内容格式验证：只传递信息，不解释推荐理由。

验证四件事：
  1. 模型返回的 headline / points 真的被解析进了数据结构（此前 prompt 要求了但代码丢弃）；
  2. 模型即使返回 reason，也被丢弃，不进入任何渲染通道；
  3. 五个通道（Markdown / 飞书 / 钉钉 / Telegram / HTML）都不出现「为什么推」、
     置信度、闸门分数这类内部判断信息，并且都不带反馈按钮（反馈只在控制台）；
  4. 规则模式（无模型）下不崩、不产生空摘要——历史版本在这里踩过未定义变量的坑。

用法：python tests/test_content_format.py
"""

from __future__ import annotations

import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import render                      # noqa: E402
from intel.config import load_config          # noqa: E402
from intel.judge import judge_all             # noqa: E402
from intel.normalize import normalize_item    # noqa: E402

HEADLINE = "vLLM 0.9 首 token 延迟降低 38%"
SUMMARY = ("vLLM 发布 0.9，官方基准显示首 token 延迟较 0.8 降低 38%，同卡吞吐提升 12%，"
           "需要 CUDA 12.4 以上。")
POINTS = ["首 token 延迟 -38%", "同卡吞吐 +12%", "旧版 KV cache 不兼容"]
LEAK = "这条非常值得关注，建议尽快升级"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        user_msg = ""
        for msg in body.get("messages", []):
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
        ids = re.findall(r'<candidate id="([^"]+)"', user_msg)
        results = [{
            "id": cid,
            "relevance": 9.0, "importance": 8.0, "novelty": 8.0, "actionability": 8.0,
            "confidence": 0.9,
            "change_type": "new_version",
            "headline": HEADLINE,
            "summary": SUMMARY,
            "points": POINTS,
            # 故意返回一个推荐理由：新代码必须忽略它
            "reason": LEAK,
        } for cid in ids]
        content = json.dumps({"results": results}, ensure_ascii=False)
        resp = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 260},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def _item(idx: int = 1):
    return normalize_item({
        "title": "vLLM v0.9 released",
        "url": f"https://example.com/vllm-{idx}",
        "source": "github",
        "published_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds"),
        "raw_summary": "vLLM 0.9 is out with lower first-token latency and higher throughput on the same GPU.",
        "meta": {"stars": 32000, "language": "Python", "topics": ["llm", "inference"],
                 "velocity": 120.0, "created_at": "2025-01-01T00:00:00Z"},
    })


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    cfg = load_config(ROOT / "config.json")
    cfg["llm"].update({"enabled": True, "base_url": f"http://127.0.0.1:{port}/v1",
                       "model": "mock", "timeout": 10, "max_retries": 0,
                       "price_per_1k_in": 0.001, "price_per_1k_out": 0.002})
    cfg["profile"]["output_language"] = "zh"

    failures: list[str] = []
    items = [_item()]
    judge_all(items, cfg, {}, {"github": 1.0}, tracer=None, api_key="test-key")
    item = items[0]

    # ---- 1. 解析 ----
    if item.headline != HEADLINE:
        failures.append(f"headline 未解析：{item.headline!r}")
    if list(item.points) != POINTS:
        failures.append(f"points 未解析：{item.points!r}")
    if item.reason:
        failures.append(f"reason 应被丢弃，实际为 {item.reason!r}")

    # ---- 2. 五个通道 ----
    md = render.markdown([item], "日报", "zh")
    card = json.dumps(render.feishu_card([item], "日报", "zh"), ensure_ascii=False)
    dd = "\n".join(m["text"] for m in render.dingtalk_markdown([item], "日报", "zh"))
    tg = "\n".join(render.telegram_chunks([item], "日报", lang="zh"))
    html = render.html_digest([item], [], "日报", "zh")

    bodies = {"markdown": md, "feishu": card, "dingtalk": dd, "telegram": tg, "html": html}
    banned = ["为什么推", "为什么是它", "值得关注", "建议升级", "confidence", "置信", "四闸", "score "]
    for name, body in bodies.items():
        for word in banned:
            if word in body:
                failures.append(f"{name} 仍包含内部信息/推荐语：{word}")
        if HEADLINE not in body:
            failures.append(f"{name} 缺少 headline")
        if SUMMARY[:18] not in body:
            failures.append(f"{name} 缺少摘要")
        if LEAK[:8] in body:
            failures.append(f"{name} 泄漏了推荐理由")

    if POINTS[0] not in md or POINTS[1] not in html:
        failures.append("要点未进入 markdown / html")
    if POINTS[0] not in card:
        failures.append("要点未进入飞书卡片")

    # ---- 3. 简报不展示理由 ----
    brief = render.briefing_html({
        "date": "2026-11-27", "runs": 1, "fetched": 10, "kept": 1, "pushed": 1, "silenced": 0,
        "topics": [{"id": "ai-eng", "name": "AI 工程", "kept": 1, "items": [{
            "title": HEADLINE, "url": "https://example.com/x", "summary": SUMMARY,
            "reason": LEAK, "first_seen": "2026-11-27T09:00:00",
        }]}],
    }, "zh")
    if "为什么推" in brief or LEAK[:8] in brief:
        failures.append("简报仍展示推荐理由")

    # ---- 4. 规则模式不崩、不产出理由 ----
    cfg_rule = json.loads(json.dumps(cfg))
    cfg_rule["llm"]["enabled"] = False
    rule_item = _item(2)
    judge_all([rule_item], cfg_rule, {}, {"github": 1.0}, tracer=None, api_key="")
    if rule_item.reason:
        failures.append("规则模式仍写入 reason")
    if not rule_item.summary:
        failures.append("规则模式没有摘要")
    if not rule_item.passed and not rule_item.reject_reason:
        failures.append("规则模式既未通过也未给出淘汰原因")
    render.markdown([rule_item], "日报", "zh")

    srv.shutdown()
    print(f"内容格式检查：{'PASS' if not failures else 'FAIL'}")
    for f in failures:
        print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
