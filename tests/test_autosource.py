"""兜底选源测试：没配置源时自动挑源，只认可信域名，且必须真抓通。

验证九件事：
  1. 只有四类源全空才触发；配了任意一个源就不介入；
  2. 没有模型时按目录标签匹配（金融 → 财经源，AI → 官方博客/预印本）；
  3. 模型推荐的域名不在可信白名单里 → 拒，并给出理由；
  4. 白名单里但抓不通 / 不是 feed / 空 feed → 拒；
  5. 条数上限与 URL 去重生效；
  6. 缓存命中不重复调模型；过期后重新挑；
  7. `apply_to_config` 只追加不覆盖，且按 URL 去重；
  8. 选出来的源能进 pipeline 的采集配置（真跑一轮 run_once）；
  9. 配置校验拦住越界的 max_feeds / ttl_hours。

全部离线：校验用本地 HTTP 服务，模型用假 chat_fn，不发任何外网请求。

用法：python tests/test_autosource.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import autosource, pipeline, topics      # noqa: E402
from intel.config import load_config, validate      # noqa: E402
from intel.store import Store                       # noqa: E402

RSS_OK = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Fake Feed</title>
<item><title>降准 0.25 个百分点</title><link>https://example.com/a</link>
<description>释放长期资金 5000 亿元</description><pubDate>Fri, 27 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>国债收益率跌破 2.0%</title><link>https://example.com/b</link>
<description>10 年期下行 4.5bp</description><pubDate>Fri, 27 Nov 2026 07:00:00 GMT</pubDate></item>
<item><title>黄金站上 3400 美元</title><link>https://example.com/c</link>
<description>年内涨 28%</description><pubDate>Fri, 27 Nov 2026 06:00:00 GMT</pubDate></item>
</channel></rss>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/feed.xml"):
            body = RSS_OK.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
        elif self.path.startswith("/page"):
            body = b"<html><body>" + b"hello " * 100 + b"</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _empty_cfg(tmp: Path) -> dict:
    cfg = copy.deepcopy(load_config(ROOT / "config.json"))
    cfg["storage"].update(db=str(tmp / "t.db"), out_dir=str(tmp / "out"),
                          trace_dir=str(tmp / "traces"))
    cfg["llm"]["enabled"] = False
    cfg["sources"]["github"]["enabled"] = False
    cfg["sources"]["hn"]["enabled"] = False
    cfg["sources"]["rss"] = []
    cfg["sources"]["page"] = {"enabled": False, "targets": []}
    cfg["sources"]["auto"] = {**autosource.DEFAULTS, "validate": False}
    cfg["channels"] = {k: {**v, "enabled": False}
                       for k, v in cfg["channels"].items() if isinstance(v, dict)}
    return cfg


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{name}{('：' + detail) if detail else ''}")

    tmp = Path(tempfile.mkdtemp(prefix="autosource-"))
    cfg = _empty_cfg(tmp)

    # ---- 1. 触发条件 ----
    check("源全空时触发", autosource.needs_discovery(cfg), str(autosource.enabled_source_count(cfg)))
    with_feed = copy.deepcopy(cfg)
    with_feed["sources"]["rss"] = [{"name": "x", "url": "https://example.com/feed"}]
    check("配了一个 RSS 就不触发", not autosource.needs_discovery(with_feed))
    with_gh = copy.deepcopy(cfg)
    with_gh["sources"]["github"]["enabled"] = True
    check("开了 GitHub 就不触发", not autosource.needs_discovery(with_gh))
    off = copy.deepcopy(cfg)
    off["sources"]["auto"]["enabled"] = False
    check("关掉兜底开关就不触发", not autosource.needs_discovery(off))

    # ---- 2. 目录匹配：不同领域挑不同源 ----
    fin = copy.deepcopy(cfg)
    fin["profile"]["role"] = "做资产配置的个人投资者"
    fin["profile"]["interests"] = ["利率", "黄金", "美股"]
    fin["filter"]["keywords"] = ["美联储", "国债", "通胀"]
    fin_names = [e["name"] for e in autosource.catalog_matches(fin, limit=4)]
    check("金融主题挑到财经源",
          any(n in ("华尔街见闻", "CNBC 财经", "WSJ 市场", "BBC 商业") for n in fin_names),
          str(fin_names))
    check("金融主题不挑 arXiv", "arXiv cs.AI" not in fin_names, str(fin_names))

    ai = copy.deepcopy(cfg)
    ai["profile"]["role"] = "算法工程师"
    ai["profile"]["interests"] = ["大模型", "开源模型", "论文"]
    ai["filter"]["keywords"] = ["llm", "agent", "推理"]
    ai_names = [e["name"] for e in autosource.catalog_matches(ai, limit=4)]
    check("AI 主题挑到官方/论文源",
          any(n in ("Hugging Face 博客", "arXiv cs.AI", "Google DeepMind", "OpenAI 新闻")
              for n in ai_names), str(ai_names))

    none = copy.deepcopy(cfg)
    none["profile"] = {"role": "", "interests": [], "output_language": "zh"}
    none["filter"]["keywords"] = []
    check("完全没线索时不硬凑", autosource.catalog_matches(none) == [])

    # ---- 3. 信任过滤 ----
    ok, tier, _ = autosource.trust_ok("https://huggingface.co/blog/feed.xml")
    check("官方域名通过且是 tier 1", ok and tier == 1, f"{ok} {tier}")
    ok, tier, reason = autosource.trust_ok("https://blog.openai.com/rss.xml")
    check("子域按父域算", ok and tier == 1, reason)
    ok, _, reason = autosource.trust_ok("https://free-money.xyz/feed.xml")
    check("可疑后缀拒绝", not ok and "可疑" in reason, reason)
    ok, _, reason = autosource.trust_ok("https://random-blog.example/feed")
    check("白名单外拒绝", not ok and "白名单" in reason, reason)
    ok, tier, _ = autosource.trust_ok("https://random-blog.example/feed", allow_unknown=True)
    check("allow_unknown 放行但降为社区级", ok and tier == 3, str(tier))

    # ---- 4. 真抓校验（本地假服务）----
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    ok, count, why = autosource.validate_feed({"url": base + "/feed.xml", "name": "fake"})
    check("合法 feed 通过并数出条数", ok and count == 3, f"{ok} {count} {why}")
    ok, count, why = autosource.validate_feed({"url": base + "/page", "name": "html"})
    check("HTML 页面不算 feed", not ok, why)
    ok, count, why = autosource.validate_feed({"url": base + "/404", "name": "404"})
    check("404 被拒", not ok, why)

    # ---- 5. 模型推荐 + 信任过滤 + 上限 + 去重 ----
    def fake_chat(cfg_, api_key, messages, **kw):
        class R:
            text = json.dumps({"feeds": [
                {"url": base + "/feed.xml", "name": "假 feed", "why": "测试"},
                {"url": base + "/feed.xml", "name": "重复", "why": "重复"},
                {"url": "https://spam-farm.xyz/feed", "name": "垃圾站", "why": "x"},
                {"url": "https://huggingface.co/blog/feed.xml", "name": "HF", "why": "官方"},
            ]}, ensure_ascii=False)
            total_tokens = 120
        return R()

    llm_cfg = copy.deepcopy(cfg)
    llm_cfg["llm"]["enabled"] = True     # 模型路径要求 llm.enabled，不只是有 key
    # 本地 127.0.0.1 不在白名单，用 allow_unknown 放行（垃圾后缀仍然会被拒）
    got = autosource.discover(llm_cfg, "sk-test", use_llm=True, max_feeds=3,
                              allow_unknown=True,
                              chat_fn=fake_chat, fetch_text=lambda url, **kw: RSS_OK)
    urls = [f["url"] for f in got["feeds"]]
    check("模型路径被采用", got["source"] == "llm", got["source"])
    check("重复 URL 只留一条", urls.count(base + "/feed.xml") == 1, str(urls))
    check("白名单外被拒", any("spam-farm" in r["url"] for r in got["rejected"]),
          str(got["rejected"]))
    check("模型给的 token 记账", got["tokens"] == 120, str(got["tokens"]))
    check("条数不超过上限", len(got["feeds"]) <= 3, str(len(got["feeds"])))
    check("官方源排前面", got["feeds"][0]["tier"] <= got["feeds"][-1]["tier"],
          str([f["tier"] for f in got["feeds"]]))

    # 模型不可用时回落目录
    def dead_chat(*a, **k):
        raise RuntimeError("模型挂了")

    fallback = autosource.discover(fin, "sk-test", use_llm=True, max_feeds=2,
                                   chat_fn=dead_chat, fetch_text=lambda url, **kw: RSS_OK)
    check("模型挂了回落目录", fallback["source"] == "catalog" and fallback["feeds"],
          f"{fallback['source']} {len(fallback['feeds'])}")

    # ---- 6. 缓存 ----
    calls = {"n": 0}

    def counting_chat(*a, **k):
        calls["n"] += 1
        return fake_chat(*a, **k)

    cache_cfg = copy.deepcopy(fin)
    cache_cfg["llm"]["enabled"] = True
    cache_cfg["sources"]["auto"] = {**autosource.DEFAULTS, "validate": True, "ttl_hours": 168}
    first = autosource.ensure(cache_cfg, "sk-test", chat_fn=counting_chat,
                              fetch_text=lambda url, **kw: RSS_OK)
    second = autosource.ensure(cache_cfg, "sk-test", chat_fn=counting_chat,
                               fetch_text=lambda url, **kw: RSS_OK)
    check("第二次命中缓存", second.get("cached") is True, str(second.get("source")))
    check("缓存命中不再调模型", calls["n"] == 1, str(calls["n"]))
    cache_file = autosource._cache_path(cache_cfg)
    check("缓存落在 data 目录", cache_file.exists(), str(cache_file))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    for ent in (data.get("entries") or {}).values():
        ent["created_at"] = 0  # 强制过期
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    third = autosource.ensure(cache_cfg, "sk-test", chat_fn=counting_chat,
                              fetch_text=lambda url, **kw: RSS_OK)
    check("过期后重新挑", calls["n"] == 2 and not third.get("cached"), str(calls["n"]))

    # ---- 7. 写进配置：只追加、不覆盖、去重 ----
    base_cfg = copy.deepcopy(cfg)
    base_cfg["sources"]["rss"] = [{"name": "已有", "url": "https://wallstreetcn.com/feed"}]
    merged = autosource.apply_to_config(base_cfg, first["feeds"])
    names = [f["name"] for f in merged["sources"]["rss"]]
    check("已有源没被覆盖", names[0] == "已有", str(names))
    check("新源被追加", len(names) > 1, str(names))
    again = autosource.apply_to_config(merged, first["feeds"])
    check("重复写入不产生重复源", len(again["sources"]["rss"]) == len(names),
          f"{len(again['sources']['rss'])} vs {len(names)}")

    # ---- 8. pipeline 里真的用上了 ----
    run_cfg = copy.deepcopy(cfg)
    run_cfg["llm"]["enabled"] = False
    captured: dict = {}

    def fake_fetch(cfg_, store, tracer, offline_items, plan=None):
        captured["rss"] = [f.get("name") for f in (cfg_["sources"].get("rss") or [])]
        return [], []

    orig_ensure, orig_fetch = autosource.ensure, pipeline.fetch_all
    autosource.ensure = lambda *a, **k: {"feeds": [
        {"name": "自动源A", "url": "https://wallstreetcn.com/feed", "tier": 2,
         "validated_items": 10, "trust": "主流媒体"},
        {"name": "自动源B", "url": "https://huggingface.co/blog/feed.xml", "tier": 1,
         "validated_items": 8, "trust": "官方"}],
        "source": "catalog", "rejected": []}
    pipeline.fetch_all = fake_fetch
    try:
        res = pipeline.run_once(run_cfg, Store(run_cfg["storage"]["db"]),
                                dry_run=True, offline=False, verbose=False)
    finally:
        autosource.ensure, pipeline.fetch_all = orig_ensure, orig_fetch
    check("pipeline 把自动源加进了采集配置",
          captured.get("rss") == ["自动源A", "自动源B"], str(captured))
    check("结果里留下选源记录",
          len((res.get("auto_sources") or {}).get("feeds") or []) == 2,
          str(res.get("auto_sources")))

    # ---- 9. 配置校验 ----
    bad = copy.deepcopy(cfg)
    bad["sources"]["auto"]["max_feeds"] = 99
    errs = validate(bad)
    check("max_feeds 越界报错", any("max_feeds" in e for e in errs), str(errs))
    bad2 = copy.deepcopy(cfg)
    bad2["sources"]["auto"]["ttl_hours"] = 9999
    check("ttl_hours 越界报错", any("ttl_hours" in e for e in validate(bad2)))

    srv.shutdown()

    if failures:
        print(f"FAIL {len(failures)} 项：")
        for f in failures:
            print("  -", f)
        return 1
    print("OK 兜底选源 9 组断言全部通过")
    print("临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
