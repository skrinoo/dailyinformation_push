"""端到端验证：真实 pipeline + mock LLM，检查跨轮次行为。

重点不是「跑通一次」，而是三件跨轮次才能暴露的事：
  1. 第二轮不重复产出（水位线 + 幂等真的生效）；
  2. 连续零产出会被计数（心跳的触发条件）；
  3. LLM 判定结果能正确流到配额、落库与报告。

用法：python tests/test_e2e.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from intel.config import load_config     # noqa: E402
from intel.pipeline import run_once      # noqa: E402
from intel.store import Store            # noqa: E402
from test_llm_path import STATE, _server  # noqa: E402


def _cfg(tmp: Path, port: int) -> dict:
    cfg = load_config(ROOT / "config.json")
    cfg["storage"] = {
        "db": str(tmp / "e2e.db"),
        "out_dir": str(tmp / "out"),
        "trace_dir": str(tmp / "traces"),
    }
    cfg["llm"].update({
        "enabled": True,
        "base_url": f"http://127.0.0.1:{port}/v1",
        "model": "mock-model",
        "timeout": 10,
        "max_retries": 0,
        "budget_usd_per_run": 1.0,
    })
    cfg["channels"]["heartbeat"]["enabled"] = False  # 心跳不在此处发网络请求
    for name in ("feishu", "telegram", "dingtalk"):
        cfg["channels"][name]["enabled"] = False
    return cfg


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    os.environ["LLM_API_KEY"] = "test-key"  # pipeline 从环境变量取，不写进配置文件
    srv = _server()
    port = srv.server_address[1]
    STATE["mode"] = "ok"
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cfg = _cfg(tmp, port)
        store = Store(cfg["storage"]["db"])

        # ---- 第 1 轮 ----
        r1 = run_once(cfg, store, dry_run=True, offline=True,
                      fixtures=str(ROOT / "tests/fixtures/sample_items.json"),
                      verbose=False)
        if r1.get("kept", 0) <= 0:
            failures.append(f"第 1 轮应产出条目，实际 kept={r1.get('kept')}")
        if not Path(r1.get("html", "")).exists():
            failures.append("第 1 轮未生成 HTML 报告")
        html = Path(r1["html"]).read_text(encoding="utf-8")
        if "为什么推" in html:
            failures.append("HTML 报告仍包含「为什么推」——应只陈述事实")
        if "淘汰明细" not in html:
            failures.append("HTML 报告缺少淘汰明细")
        if r1["judge"]["llm_ok"] <= 0:
            failures.append(f"LLM 未被调用：{r1['judge']}")

        # ---- 第 2 轮：同一批数据不应重复产出 ----
        r2 = run_once(cfg, store, dry_run=True, offline=True,
                      fixtures=str(ROOT / "tests/fixtures/sample_items.json"),
                      verbose=False)
        if r2.get("kept", 0) != 0:
            failures.append(f"第 2 轮应无新增，实际 kept={r2.get('kept')}（幂等失效）")
        if not store.consecutive_empty_runs() >= 1:
            failures.append("零产出一轮后，连续空跑计数未增加")

        # ---- 第 3/4 轮：连续零产出应达心跳阈值 ----
        empty_fixture = tmp / "empty.json"
        empty_fixture.write_text(json.dumps({"items": []}, ensure_ascii=False), encoding="utf-8")
        run_once(cfg, store, dry_run=True, offline=True, fixtures=str(empty_fixture), verbose=False)
        run_once(cfg, store, dry_run=True, offline=True, fixtures=str(empty_fixture), verbose=False)
        if store.consecutive_empty_runs() < 2:
            failures.append(f"连续空跑计数={store.consecutive_empty_runs()}（心跳阈值 2 未达成）")

        # ---- 判定结果是否落库 ----
        rows = store.conn.execute(
            "SELECT COUNT(*) c FROM items WHERE verdict = 'selected'"
        ).fetchone()["c"]
        if rows <= 0:
            failures.append("selected 条目未落库")

        gates_rows = store.conn.execute(
            "SELECT gates FROM items WHERE verdict = 'selected' LIMIT 1"
        ).fetchone()
        if not gates_rows or not json.loads(gates_rows["gates"]):
            failures.append("落库的 gates 为空")

        runs = store.recent_runs(10)
        if not runs:
            failures.append("runs 表没有记录")

        store.close()

    srv.shutdown()
    print(f"端到端检查：{'PASS' if not failures else 'FAIL'}")
    for f in failures:
        print(f"  - {f}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
