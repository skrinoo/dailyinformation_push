"""生成一份离线样例日报，用来看新的内容格式（不联网、不调用真实模型）。

用途：改完提示词与渲染后，快速目视检查「标题 + 摘要 + 2-3 条要点」的实际排版，
不需要等到早上八点半，也不需要 API key。

模型输出由本地 mock 用候选原文的句子拼出来——不是真实模型写的，但内容全部来自
样本原文，不编造数字，足以验证版式与「无推荐理由」这条约束。

用法：python tests/make_demo.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel.config import load_config          # noqa: E402
from intel.pipeline import run_once           # noqa: E402
from intel.store import Store                 # noqa: E402

PREFIX = re.compile(r"^(show hn|ask hn|tell hn|introducing|announcing)\s*[:：-]\s*", re.I)

# 演示文案：内容取自样本原文，按样本标题匹配。真实运行时这些由模型生成。
DEMO_COPY = [
    ("openwrt", ("OpenWrt 25.12 默认改用 nftables",
                 "OpenWrt 25.12 将防火墙后端从 iptables 换成 nftables，mt7621 上 WireGuard 吞吐提升约 40%，"
                 "LuCI 面板重写。升级需手动迁移旧规则。",
                 ["nftables 成为默认后端", "WireGuard 吞吐 +40%", "旧 iptables 规则需迁移"])),
    ("anthropic publishes", ("Anthropic 发布 agent 构建指南",
                 "指南拆解五种 agent 模式：提示链、路由、并行、编排-工作者、评估-优化，"
                 "并给出不该用 agent 的场景与判断标准。",
                 ["覆盖 5 种编排模式", "含反例与适用边界", "每种模式附代码示例"])),
    ("arm-infer", ("arm-infer 树莓派吞吐达 llama.cpp 2.1 倍",
                 "C++20 推理引擎，INT4 内核加工作窃取调度器，官方基准在 Raspberry Pi 上"
                 "达到 llama.cpp 的 2.1 倍 tokens/s。",
                 ["INT4 内核 + 工作窃取", "树莓派 2.1x tokens/s", "需 ARMv8.2 以上"])),
    ("memory store", ("本地优先的 agent 记忆库开源",
                 "用 SQLite 加向量检索存事实，每条事实是可编辑的行而不是原始对话，"
                 "并提供 diff 视图查看 agent 到底改了什么。",
                 ["事实存为可编辑行", "内置 diff 视图", "纯本地，无服务端"])),
    ("agent-framework", ("微软开源多智能体编排框架",
                 "覆盖构建、编排与部署三个阶段，内置工具调用、类型化记忆与人工审批节点，"
                 "附带图运行时与示例工程。",
                 ["工具调用 + 类型化记忆", "内置人工审批节点", "Apache-2.0 许可"])),
]
CJK = re.compile(r"[\u4e00-\u9fff]")


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

        if "情报编辑" not in system:
            content = json.dumps({"queries": []}, ensure_ascii=False)
        else:
            results = []
            for cid, block in re.findall(r'<candidate id="([^"]+)">(.*?)</candidate>', user, re.S):
                title = (re.search(r"^title: (.*)$", block, re.M) or [None, ""])[1].strip()
                raw = (re.search(r"^summary: (.*)$", block, re.M | re.S) or [None, ""])[1]
                raw = re.sub(r"\s+", " ", raw).strip()
                sentences = [s.strip() for s in
                             re.split(r"(?<=[。！？])|(?<=[.!?])\s+", raw) if len(s.strip()) > 8]
                demo = next((c for key, c in DEMO_COPY if key in title.lower()), None)
                if demo:
                    headline, summary, points = demo
                else:
                    headline = PREFIX.sub("", title).strip()
                    headline = headline if len(headline) <= 30 else headline[:28] + "…"
                    summary, used = "", 0
                    for sent in sentences:
                        if used >= 60 or (summary and used + len(sent) > 130):
                            break
                        summary = (summary + " " + sent).strip()
                        used += len(sent)
                    points = [sent[:18] for sent in sentences if re.search(r"\d", sent)][:3]
                results.append({
                    "id": cid,
                    "relevance": 8.0, "importance": 7.5, "novelty": 7.0, "actionability": 7.0,
                    "confidence": 0.82, "change_type": "first_seen",
                    "headline": headline,
                    "summary": summary or headline,
                    "points": points,
                })
            content = json.dumps({"results": results}, ensure_ascii=False)

        resp = json.dumps({"choices": [{"message": {"content": content}}],
                           "usage": {"prompt_tokens": 900, "completion_tokens": 400}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    cfg = load_config(ROOT / "config.json")
    os.environ["LLM_API_KEY"] = "demo-key"
    cfg["llm"].update({"enabled": True, "api_key_env": "LLM_API_KEY",
                       "base_url": f"http://127.0.0.1:{port}/v1",
                       "model": "demo", "timeout": 15, "max_retries": 0,
                       "price_per_1k_in": 0, "price_per_1k_out": 0})
    cfg["topics"] = []
    cfg["filter"]["silence"] = {"enabled": False, "min_confidence": 0.0}
    cfg["output"]["creator"] = {"enabled": True, "min_score": 0, "max_candidates": 3}
    cfg["channels"] = {k: {**v, "enabled": False} if isinstance(v, dict) else v
                       for k, v in cfg["channels"].items()}

    with tempfile.TemporaryDirectory() as tmp:
        cfg["storage"] = {**cfg["storage"], "db": str(Path(tmp) / "demo.db"),
                          "out_dir": str(ROOT / "out")}
        store = Store(cfg["storage"]["db"])
        result = run_once(cfg, store, dry_run=True, offline=True,
                          fixtures=str(ROOT / "tests/fixtures/sample_items.json"), verbose=True)
        store.close()

    srv.shutdown()
    print("\nMarkdown:", result.get("md"))
    print("HTML:", result.get("html"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
