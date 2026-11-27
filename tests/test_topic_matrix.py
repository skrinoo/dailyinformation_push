"""关注项矩阵测试：不同主题 + 不同用户提示词，互不串味。

验证七件事：
  1. 关键词筛选真的按主题生效（AI 主题捞不到 OpenWrt，反之亦然）；
  2. 同一批夹具，不同主题入选集合两两不相交；
  3. 屏蔽词优先于关键词（含「C++」的招聘贴不会进 C++ 主题）；垃圾与注入贴不进任何主题；
  4. 停用的关注项既不选内容，也不进调度；
  5. 水位线按主题分区：A 主题看过同一 URL，B 主题仍能拿到；
  6. 同一主题连跑两次，第二次 0 新增（幂等）；
  7. 用户提示词（custom_instructions / role / interests / avoid）完整进当前主题的判定
     prompt，且不夹带其他主题的。

全部离线跑，用临时库与临时产物目录，不碰 data/ 与 out/。

用法：python tests/test_topic_matrix.py
"""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import judge, topics                     # noqa: E402
from intel.config import load_config                # noqa: E402
from intel.pipeline import run_once                 # noqa: E402
from intel.store import Store                       # noqa: E402

FIXTURES = "tests/fixtures/sample_items.json"

TOPICS = [
    {"id": "ai-agent", "name": "AI Agent 工程", "enabled": True,
     "role": "做 LLM 应用的工程师",
     "keywords": ["agent", "llm", "memory"],
     "block_keywords": ["airdrop", "hiring"],
     "interests": ["agent 框架", "上下文管理"],
     "avoid": ["融资新闻", "招聘"],
     "custom_instructions": "只关心框架与协议变化，融资与人事不写。"},
    {"id": "net-openwrt", "name": "路由器固件", "enabled": True,
     "role": "自己刷固件的网络玩家",
     "keywords": ["openwrt", "nftables", "wireguard"],
     "block_keywords": [],
     "interests": ["OpenWrt 版本升级"],
     "avoid": ["企业采购"],
     "custom_instructions": "只关注升级破坏性变更与回滚方式。"},
    {"id": "cpp-core", "name": "C++ 语言", "enabled": True,
     "keywords": ["c++", "cpp"],
     "block_keywords": ["hiring"],
     "custom_instructions": "只关心标准与编译器实现，不看招聘。"},
    {"id": "quant", "name": "量化推理", "enabled": True,
     "keywords": ["quantization", "量化"],
     "block_keywords": ["airdrop"],
     "custom_instructions": "只看精度损失与显存占用，不看跑分宣传。"},
    {"id": "dup-a", "name": "固件镜像 A", "enabled": True,
     "keywords": ["openwrt"], "block_keywords": []},
    {"id": "dup-b", "name": "固件镜像 B", "enabled": True,
     "keywords": ["openwrt"], "block_keywords": []},
    {"id": "en-only", "name": "English feed", "enabled": True,
     "output_language": "en", "keywords": ["openwrt"], "block_keywords": []},
    {"id": "dormant", "name": "停用项", "enabled": False,
     "keywords": ["agent"], "custom_instructions": "这条不该出现在任何 prompt 里。"},
]

AIRDROP = "airdrop"
HIRING = "hiring"
INJECTION = "ignore all previous instructions"
OPENWRT = "openwrt"


def _cfg(tmp: Path) -> dict:
    cfg = copy.deepcopy(load_config(ROOT / "config.json"))
    cfg["storage"].update(
        db=str(tmp / "matrix.db"),
        out_dir=str(tmp / "out"),
        trace_dir=str(tmp / "traces"),
    )
    cfg["llm"]["enabled"] = False
    cfg["topics"] = copy.deepcopy(TOPICS)
    # 全部渠道关掉：这轮只验筛选与提示词，不发任何东西
    cfg["channels"] = {k: {**v, "enabled": False}
                       for k, v in cfg["channels"].items() if isinstance(v, dict)}
    return cfg


def _text(row: dict) -> str:
    return " ".join(str(row.get(k) or "") for k in ("title", "summary", "points")).lower()


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{name}{('：' + detail) if detail else ''}")

    tmp = Path(tempfile.mkdtemp(prefix="topicmatrix-"))
    cfg = _cfg(tmp)
    db = cfg["storage"]["db"]

    # ---- 逐个主题真跑一遍（同一库，故意让它们共享水位线表）----
    picked: dict[str, list[dict]] = {}
    for topic in TOPICS:
        if not topic.get("enabled", True):
            continue
        run_once(cfg, Store(db), topic=topic["id"], dry_run=True, offline=True,
                 fixtures=FIXTURES, force_rule=True, verbose=False)
        picked[topic["id"]] = Store(db).topic_items("2026-01-01", topic=topic["id"])

    st = Store(db)

    # ---- 1. 每个启用主题都要有产出 ----
    for tid in ("ai-agent", "net-openwrt", "cpp-core", "quant"):
        check(f"{tid} 有入选", len(picked.get(tid) or []) >= 1,
              f"{len(picked.get(tid) or [])} 条")
    print("入选分布：")
    for tid, rows in picked.items():
        print(f"  {tid:<12} {len(rows)} 条")
        for row in rows:
            print(f"      · {row['title'][:56]}")

    # ---- 2. 入选内容必须命中本主题关键词 ----
    by_id = {t["id"]: t for t in TOPICS}
    for tid, rows in picked.items():
        words = [w.lower() for w in (by_id[tid].get("keywords") or [])]
        for row in rows:
            text = _text(row)
            check(f"{tid} 条目命中本主题关键词", any(w in text for w in words),
                  f"「{row['title'][:40]}」正文里没有 {'/'.join(words)}")

    # ---- 3. 主题之间不许串 ----
    # dup-a / dup-b 是故意用同一关键词的两个主题，用来验证水位线分区，
    # 它们共享 URL 是设计要求，不参与这条互斥检查。
    distinct = ("ai-agent", "net-openwrt", "cpp-core", "quant")
    seen: dict[str, str] = {}
    for tid in distinct:
        for row in picked.get(tid) or []:
            url = str(row.get("canonical_url") or row.get("url") or "")
            if url in seen:
                failures.append(f"同一 URL 出现在两个主题：{tid} 与 {seen[url]}（{url[:60]}）")
            seen[url] = tid

    check("net-openwrt 抓到 OpenWrt 那条",
          any(OPENWRT in _text(r) for r in picked.get("net-openwrt") or []))
    check("ai-agent 不掺 OpenWrt",
          not any(OPENWRT in _text(r) for r in picked.get("ai-agent") or []))
    check("cpp-core 不掺 AI agent",
          not any("agent" in _text(r) for r in picked.get("cpp-core") or []))

    # ---- 4. 屏蔽词与安全 ----
    everything = [r for rows in picked.values() for r in rows]
    check("空投贴不进任何主题",
          not any(AIRDROP in _text(r) for r in everything))
    check("招聘贴不进 ai-agent / cpp-core",
          not any(HIRING in _text(r)
                  for tid in ("ai-agent", "cpp-core") for r in picked.get(tid) or []))
    check("注入贴不进任何主题",
          not any(INJECTION in _text(r) for r in everything))

    # 招聘贴含 C++ 却被屏蔽，正好证明「屏蔽词优先于关键词」
    check("含 C++ 的招聘贴被屏蔽词拦住",
          not any("senior c++ engineer" in _text(r) for r in picked.get("cpp-core") or []))

    # ---- 5. 停用项不选也不排 ----
    check("停用项无产出", not st.topic_items("2026-01-01", topic="dormant"))
    check("停用项不在调度里", "dormant" not in topics.enabled_ids(cfg),
          str(topics.enabled_ids(cfg)))

    # ---- 6. 水位线：同一 URL 在两个主题下各算各的 ----
    check("dup-a 拿到 OpenWrt", len(picked.get("dup-a") or []) >= 1,
          f"{len(picked.get('dup-a') or [])} 条")
    check("dup-b 也拿到 OpenWrt（没被 dup-a 吃掉）",
          len(picked.get("dup-b") or []) >= 1,
          f"{len(picked.get('dup-b') or [])} 条")

    # ---- 7. 同一主题连跑两次，第二次 0 新增 ----
    before = len(st.topic_items("2026-01-01", topic="ai-agent"))
    run_once(cfg, Store(db), topic="ai-agent", dry_run=True, offline=True,
             fixtures=FIXTURES, force_rule=True, verbose=False)
    after = len(Store(db).topic_items("2026-01-01", topic="ai-agent"))
    check("同一主题重跑无新增", after == before, f"{before} → {after}")

    # ---- 8. 用户提示词：各进各的 prompt ----
    prompts: dict[str, str] = {}
    for topic in TOPICS:
        effective = topics.effective(cfg, topic["id"])
        prompts[topic["id"]] = judge._build_user_prompt([], effective, {})

    global_role = (cfg.get("profile") or {}).get("role") or "未填写"
    for topic in TOPICS:
        tid = topic["id"]
        custom = str(topic.get("custom_instructions") or "")
        if custom:
            check(f"{tid} 的附加要求进了判定 prompt", custom in prompts[tid],
                  prompts[tid][:80])
        # 没填 role 的主题继承全局 role，这是设计行为，不是漏传
        role = topic.get("role") or global_role
        check(f"{tid} 的角色进了判定 prompt", f"角色：{role}" in prompts[tid],
              prompts[tid].split("\n")[1] if "\n" in prompts[tid] else "")

    # 互不夹带：停用项那句当探针，除它自己外任何 prompt 里都不该出现
    for tid, text in prompts.items():
        if tid == "dormant":
            continue
        check(f"{tid} 没夹带其他主题的提示词",
              "这条不该出现在任何 prompt 里" not in text)

    # ---- 9. 覆盖白名单与列表替换语义 ----
    eff = topics.effective(cfg, "cpp-core")
    check("keywords 是整体替换", eff["filter"]["keywords"] == ["c++", "cpp"],
          str(eff["filter"]["keywords"]))
    sneaky = copy.deepcopy(cfg)
    for topic in sneaky["topics"]:
        if topic["id"] == "cpp-core":
            topic["storage"] = {"db": "/tmp/should-not-apply.db"}
    check("关注项改不动 storage", topics.effective(sneaky, "cpp-core")["storage"]["db"] == db,
          topics.effective(sneaky, "cpp-core")["storage"]["db"])

    # ---- 10. 输出语言跟着关注项走 ----
    from intel.i18n import resolve_lang
    from intel import render

    en_cfg = topics.effective(cfg, "en-only")
    zh_cfg = topics.effective(cfg, "ai-agent")
    check("en-only 用英文", resolve_lang(en_cfg) == "en", resolve_lang(en_cfg))
    check("ai-agent 仍是中文", resolve_lang(zh_cfg) == "zh", resolve_lang(zh_cfg))
    sample = picked["en-only"][0] if picked.get("en-only") else None
    if sample:
        from intel.models import Item

        item = Item(id="lang-probe", title=sample["title"], url=sample["url"],
                    canonical_url=sample.get("canonical_url") or sample["url"],
                    source=sample.get("source") or "", summary="")
        en_md = render.markdown([item], "Digest", "en")
        zh_md = render.markdown([item], "日报", "zh")
        check("英文渲染用英文标签", "Source:" in en_md and "原文" not in en_md,
              en_md[:60].replace("\n", " "))
        check("中文渲染用中文标签", "来源：" in zh_md, zh_md[:60].replace("\n", " "))

    if failures:
        print("\nFAIL")
        for line in failures:
            print("  - " + line)
        return 1
    print("\nOK 关注项矩阵 10 组检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
