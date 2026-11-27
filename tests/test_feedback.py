"""反馈按关注项记账：验证十件事。

  1. 反馈落在条目所属关注项，不动全局权重
  2. 另一个关注项的权重完全不受影响（不串味）
  3. 有效权重 = 全局 × 主题，并夹在 0.5–2.5
  4. 没有主题归属的条目，反馈仍然落到全局（向后兼容）
  5. 同一来源连续 up/down 只改自己的桶
  6. feedback_log 记下了 topic，能回溯
  7. 粗排真的用上了主题权重（权重高的主题里同一来源分数更高）
  8. 参数校验：未知条目、非法 action 都报错
  9. 「没用」的原因分档：重复不扣、太浅 0.92、不可信 0.82，未知原因与 up+原因都报错
 10. 原因落了库，能按原因统计分布（控制台那块横条的数据源）

跑法：python tests/test_feedback.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel.store import Store  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        print(f"  OK   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}  {detail}")


def _seed(store: Store) -> None:
    for iid, topic, source in (
        ("a1", "macro", "wallstreetcn"),
        ("a2", "macro", "cnbc"),
        ("a3", "macro", "wsj"),
        ("a4", "macro", "ft"),
        ("b1", "ai-agent", "wallstreetcn"),
        ("g1", "", "wallstreetcn"),
    ):
        store.conn.execute(
            "INSERT INTO items (id, canonical_url, title, url, source, topic, verdict, "
            "feedback, first_seen, headline, summary, points) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, f"https://e.com/{iid}", f"title {iid}", f"https://e.com/{iid}",
             source, topic, "selected", 0, "2026-11-27T00:00:00", "", "", ""))
    store.conn.commit()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fb-"))
    try:
        db = tmp / "t.db"
        store = Store(str(db))
        _seed(store)

        print("\n[1] 反馈落在条目所属关注项，不动全局")
        out = store.add_feedback("a1", "up")
        check("scope 是条目所属主题", out["scope"] == "macro", out)
        check("主题权重被抬高", out["topic_weight"] > 1.0, out)
        check("全局权重没被改", all(w == 1.0 for w in store.source_weights().values()),
              store.source_weights())
        check("主题有效权重 > 1", store.source_weights("macro").get("wallstreetcn", 1) > 1.0,
              store.source_weights("macro"))

        print("\n[2] 另一个关注项不受影响")
        check("ai-agent 仍是 1.0", store.source_weights("ai-agent").get("wallstreetcn", 1) == 1.0,
              store.source_weights("ai-agent"))
        check("主题权重明细只有 macro 有记录",
              [d["source"] for d in store.topic_weights("ai-agent")] == [],
              store.topic_weights("ai-agent"))

        print("\n[3] 有效权重被夹住")
        for _ in range(40):
            store.add_feedback("a1", "down")
        eff = store.source_weights("macro")["wallstreetcn"]
        check("下限不低于 0.5", eff >= 0.5 - 1e-9, eff)
        for _ in range(80):
            store.add_feedback("a1", "up")
        eff = store.source_weights("macro")["wallstreetcn"]
        check("上限不超过 2.5", eff <= 2.5 + 1e-9, eff)

        print("\n[4] 无主题条目仍然记到全局")
        out = store.add_feedback("g1", "down")
        check("scope 为空", out["scope"] == "", out)
        check("全局权重变了", store.source_weights()["wallstreetcn"] != 1.0,
              store.source_weights())

        print("\n[5] 只影响自己的桶")
        store.add_feedback("b1", "up")
        macro = store.source_weights("macro")["wallstreetcn"]
        ai = store.source_weights("ai-agent")["wallstreetcn"]
        check("两个主题的系数不同", abs(macro - ai) > 1e-6, (macro, ai))
        gw = store.source_weights()["wallstreetcn"]
        tw = next(d["topic_weight"] for d in store.topic_weights("ai-agent")
                  if d["source"] == "wallstreetcn")
        check("ai-agent 的有效权重 = 全局 × 主题",
              abs(ai - min(2.5, max(0.5, gw * tw))) < 1e-6, (ai, gw, tw))

        print("\n[6] feedback_log 记了 topic")
        rows = store.conn.execute(
            "SELECT topic, action FROM feedback_log ORDER BY id").fetchall()
        tagged = [r for r in rows if r["topic"]]
        check("有带主题的记录", len(tagged) == len(rows) - 1, rows[:3])
        check("全局那条 topic 为空", any(not r["topic"] for r in rows))

        print("\n[7] 粗排真的用上主题权重")
        from intel.coarse import score_item
        from intel.models import Item
        import json

        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        item = Item(id="probe", title="利率决议与国债收益率", url="https://e.com/p",
                    canonical_url="https://e.com/p", source="wallstreetcn",
                    published_at="2026-11-27T00:00:00", raw_summary="美联储降息讨论")
        hi = score_item(item, cfg["filter"], store.source_weights("macro").get("wallstreetcn", 1.0))
        lo = score_item(item, cfg["filter"], 0.5)
        check("高权重分数更高", hi > lo, (hi, lo))

        print("\n[8] 参数校验")
        try:
            store.add_feedback("nope", "up")
            check("未知条目要报错", False)
        except KeyError:
            check("未知条目要报错", True)
        try:
            store.add_feedback("a2", "meh")
            check("非法 action 要报错", False)
        except ValueError:
            check("非法 action 要报错", True)

        print("\n[9] 原因分档")
        out = store.add_feedback("a4", "down", reason="duplicate")
        check("重复不扣权重", out["topic_weight"] == 1.0 and out["factor"] == 1.0, out)
        check("原因标签回传", out["reason_label"] == "已经看过、重复", out)
        out = store.add_feedback("a4", "down", reason="unreliable")
        check("来源不可信 ×0.82", out["topic_weight"] == 0.82, out)
        out = store.add_feedback("a3", "down", reason="shallow")
        check("太浅 ×0.92", out["topic_weight"] == 0.92, out)
        out = store.add_feedback("a2", "down")   # 不选原因保持老行为
        check("不选原因仍是 ×0.90", out["topic_weight"] == 0.9, out)

        try:
            store.add_feedback("a2", "down", reason="made-up")
            check("未知原因要报错", False)
        except ValueError:
            check("未知原因要报错", True)
        try:
            store.add_feedback("a2", "up", reason="shallow")
            check("只有没用才要原因", False)
        except ValueError:
            check("只有没用才要原因", True)

        print("\n[10] 原因分布可统计")
        stats = {r["code"]: r for r in store.reason_stats("macro")}
        check("重复 1 次", stats.get("duplicate", {}).get("count") == 1, stats)
        check("不可信 1 次", stats.get("unreliable", {}).get("count") == 1, stats)
        check("太浅 1 次", stats.get("shallow", {}).get("count") == 1, stats)
        check("没选原因的也计数", stats.get("", {}).get("count", 0) >= 2, stats)
        total = sum(r["count"] for r in stats.values())
        check("占比加起来接近 1", abs(sum(r["share"] for r in stats.values()) - 1) < 0.01,
              (total, stats))
        row = next(r for r in store.recent_items(50, "macro") if r["id"] == "a4")
        check("最近一条记下了原因", row["last_reason"] == "unreliable", row)

        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\nFAIL 反馈测试 {len(failures)} 项未通过：{failures}")
        return 1
    print("\nOK 反馈按主题记账 + 原因分档 10 组检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
