"""钉钉通道验证：加签、payload 结构、错误处理、分段、dispatch 接线。

不 mock urllib，而是起一个本地 HTTP server 当真钉钉，让「签名拼进 URL →
POST → 判定 errcode → 标记幂等」这条真实路径整条跑一遍。
这样签名算法写错了、参数名拼错了、errcode 判断漏了，都会在这里暴露。

用法：python tests/test_dingtalk.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import dingtalk, push, render        # noqa: E402
from intel.models import CHANNEL_DAILY, Item   # noqa: E402

HEADLINE = "vLLM 0.9 发布，首 token 延迟降 38%"
SUMMARY = "官方基准显示首 token 延迟较 0.8 降低 38%，同卡吞吐提升 12%，需要 CUDA 12.4 以上。"
POINTS = ["首 token 延迟降 38%", "同卡吞吐提升 12%", "最低 CUDA 12.4"]


class _State:
    requests: list[dict] = []
    reply: dict = {"errcode": 0, "errmsg": "ok"}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        parts = urllib.parse.urlsplit(self.path)
        _State.requests.append({
            "path": parts.path,
            "query": urllib.parse.parse_qs(parts.query),
            "body": json.loads(raw or "{}"),
        })
        data = json.dumps(_State.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静默，别污染测试输出
        pass


def _item(idx: int = 1, summary: str = SUMMARY) -> Item:
    return Item(
        id=f"item-{idx}",
        title=HEADLINE,
        url=f"https://example.com/p{idx}",
        canonical_url=f"https://example.com/p{idx}",
        source="github",
        published_at="2026-11-27T09:12:00",
        headline=HEADLINE,
        summary=summary,
        points=list(POINTS),
        change_type="new_version",
        channel=CHANNEL_DAILY,
        confidence=0.72,
    )


class _Store:
    """只实现 dispatch 用到的两个方法，顺便记录幂等标记。"""

    def __init__(self) -> None:
        self.marked: list[tuple[str, tuple[str, ...]]] = []

    def pushed_within(self, url: str, hours: int) -> bool:
        return False

    def mark_pushed(self, urls, channel: str, run_id: str) -> None:
        self.marked.append((channel, tuple(urls)))


def _cfg() -> dict:
    return {
        "profile": {"output_language": "zh"},
        "channels": {
            "dingtalk": {"enabled": True, "webhook_env": "DINGTALK_WEBHOOK",
                         "secret_env": "DINGTALK_SECRET"},
            "feishu": {"enabled": False},
            "telegram": {"enabled": False},
        },
    }


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{name}{('：' + detail) if detail else ''}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ["DINGTALK_WEBHOOK"] = (
        f"http://127.0.0.1:{port}/robot/send?access_token=TESTTOKEN")
    os.environ["DINGTALK_SECRET"] = "SECtestsecret"
    cfg = _cfg()

    # ---- 1. 加签算法 ----
    a = dingtalk.sign("SECtestsecret", 1732712345678)
    b = dingtalk.sign("SECtestsecret", 1732712345678)
    check("签名可复现", a == b, f"{a} vs {b}")
    check("时间戳原样保留", a["timestamp"] == "1732712345678", a["timestamp"])
    decoded = base64.b64decode(urllib.parse.unquote_plus(a["sign"]))
    check("签名是 32 字节 SHA256", len(decoded) == 32, f"{len(decoded)} 字节")
    check("签名已 URL 编码", "%" in a["sign"] or "+" in a["sign"] or "=" in a["sign"],
          a["sign"][:24])

    # ---- 2. dispatch 真发一次 ----
    _State.requests.clear()
    store = _Store()
    results = push.dispatch(cfg, [_item()], store, "run-1", "情报日报")
    dd = [r for r in results if r["channel"] == "dingtalk"]
    check("dispatch 返回钉钉结果", len(dd) == 1, results)
    check("dispatch 成功", bool(dd and dd[0]["ok"]), dd)
    check("实际发出 1 条", len(_State.requests) == 1, f"{len(_State.requests)} 条")

    if _State.requests:
        req = _State.requests[0]
        query = req["query"]
        check("路径正确", req["path"] == "/robot/send", req["path"])
        check("保留了 access_token", query.get("access_token") == ["TESTTOKEN"], query)
        check("URL 带 timestamp", bool(query.get("timestamp")), query)
        check("URL 带 sign", bool(query.get("sign")), query)

        body = req["body"]
        check("msgtype 是 markdown", body.get("msgtype") == "markdown", body.get("msgtype"))
        md = body.get("markdown") or {}
        check("有标题", bool(md.get("title")), md)
        check("标题不超过 32 字", len(md.get("title") or "") <= 32, md.get("title"))
        text = md.get("text") or ""
        check("含 headline", HEADLINE in text, text[:60])
        check("含摘要", SUMMARY[:18] in text)
        check("含全部要点", all(p in text for p in POINTS))
        check("含原文链接", "https://example.com/p1" in text)
        check("不含内部信息", not any(w in text for w in ("为什么推", "置信", "四闸")), text[:60])
        check("用钉钉标题层级", text.startswith("###"), text[:20])
        check("条目分隔用空行", "\n\n#### 1." in text, text[:80])

    check("标记了幂等", store.marked and store.marked[0][0] == "dingtalk", store.marked)

    # ---- 3. 业务错误要能识别 ----
    _State.reply = {"errcode": 310000, "errmsg": "sign not match"}
    results = push.dispatch(cfg, [_item(2)], _Store(), "run-2", "情报日报")
    dd = [r for r in results if r["channel"] == "dingtalk"]
    check("errcode 非 0 判失败", bool(dd and not dd[0]["ok"]), dd)
    check("错误里带返回码", "310000" in (dd[0]["detail"] if dd else ""), dd)
    check("错误里不泄漏 token", "TESTTOKEN" not in (dd[0]["detail"] if dd else ""), dd)
    _State.reply = {"errcode": 0, "errmsg": "ok"}

    # ---- 4. 凭据缺失要说清楚 ----
    saved = os.environ.pop("DINGTALK_WEBHOOK")
    results = push.dispatch(cfg, [_item(3)], _Store(), "run-3", "情报日报")
    dd = [r for r in results if r["channel"] == "dingtalk"]
    check("缺凭据判失败", bool(dd and not dd[0]["ok"]), dd)
    check("提示环境变量名", "DINGTALK_WEBHOOK" in (dd[0]["detail"] if dd else ""), dd)
    os.environ["DINGTALK_WEBHOOK"] = saved

    # ---- 5. 超长内容要分段，而不是被钉钉截断 ----
    long_items = [_item(i, summary=SUMMARY * 6) for i in range(1, 9)]
    messages = render.dingtalk_markdown(long_items, "情报日报", "zh", limit=1200)
    check("超长时分成多段", len(messages) > 1, f"{len(messages)} 段")
    check("每段都有标题", all(m["text"].startswith("###") for m in messages),
          [m["text"][:12] for m in messages])
    check("每段不超过阈值太多", all(len(m["text"]) <= 1500 for m in messages),
          [len(m["text"]) for m in messages])
    joined = "\n".join(m["text"] for m in messages)
    check("分段不丢条目", joined.count("#### ") == 8, joined.count("#### "))

    # ---- 6. 心跳走 markdown ----
    _State.requests.clear()
    push.send_heartbeat(cfg, "扫描 42 条，没有达到阈值的内容。")
    check("心跳发了一条", len(_State.requests) == 1, f"{len(_State.requests)} 条")
    if _State.requests:
        body = _State.requests[0]["body"]
        check("心跳也是 markdown", body.get("msgtype") == "markdown", body.get("msgtype"))
        check("心跳正文在", "扫描 42 条" in (body.get("markdown") or {}).get("text", ""),
              (body.get("markdown") or {}).get("text", "")[:40])

    # ---- 7. 配置校验拦住半配状态 ----
    from intel.config import load_config, validate

    bad = load_config(ROOT / "config.json")
    bad["channels"]["dingtalk"]["enabled"] = True
    bad["channels"]["dingtalk"]["webhook_env"] = ""
    check("启用但没填变量名要报错", any("dingtalk" in e for e in validate(bad)), validate(bad))

    server.shutdown()

    if failures:
        print("FAIL")
        for line in failures:
            print("  - " + line)
        return 1
    print("OK 钉钉通道 7 组检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
