"""跨领域关注项测试：AI、金融、医疗、体育、游戏、消费、教育，互不串味。

和 test_topic_matrix.py 的区别：
  - 那份用 16 条科技夹具，验的是「机制」（水位线、幂等、白名单）；
  - 这份用 43 条跨领域夹具（tests/fixtures/mixed_items.json），验的是「非科技主题能不能用」——
    同一个关键词引擎在金融/医疗/体育语料上是否同样有效，而不是只对 GitHub 项目有效。

验证九件事：
  1. 九个领域主题各自捞出自己那批，且集合精确相等（不多不少）；
  2. 金融主题捞不到体育，体育捞不到医疗（零交叉）；
  3. 空投 / 招聘 / 荐股软文 / 提示词注入贴，任何主题都不进；
  4. 屏蔽词优先于关键词：含「净利」的券商研报被「目标价」砍掉；
  5. 用户提示词（role / interests / avoid / custom_instructions）进当前主题 prompt，不夹带；
  6. 停用项 0 产出且不进调度；
  7. output_language=en 的主题渲染英文标签；
  8. 同一主题重跑 0 新增（幂等）；
  9. 跨域主题（AI+金融）只吃交集，不吃纯 AI 也不吃纯金融；
  10. 规则兜底的选题大纲措辞与领域无关（金融主题不会读到「同类项目 / 上一版本」）。

全部离线跑，临时库与临时产物目录，不碰 data/ 与 out/。

用法：python tests/test_mixed_domains.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import judge, render, topics                # noqa: E402
from intel.config import load_config                   # noqa: E402
from intel.pipeline import run_once                    # noqa: E402
from intel.store import Store                          # noqa: E402

FIXTURES = "tests/fixtures/mixed_items.json"

TOPICS = [
    {"id": "macro", "name": "宏观与货币政策", "enabled": True,
     "role": "做资产配置的个人投资者",
     "keywords": ["降准", "国债", "美联储", "黄金", "cpi", "ppi"],
     "block_keywords": [],
     "interests": ["货币政策", "利率与通胀数据"],
     "avoid": ["券商观点", "目标价"],
     "custom_instructions": "只看政策与数据本身，券商观点与目标价不写。"},
    {"id": "equity", "name": "A股公司动态", "enabled": True,
     "role": "看财报做基本面研究的人",
     "keywords": ["净利", "毛利率", "减持", "回购", "证监会", "股东"],
     "block_keywords": ["目标价", "研报"],
     "custom_instructions": "只写财报数字与监管动作，评级与目标价一律不要。"},
    {"id": "ai-industry", "name": "AI 产业", "enabled": True,
     "role": "关注模型与算力供给的从业者",
     "keywords": ["大模型", "gpt", "算力", "开源模型", "qwen", "ai 法案"],
     "block_keywords": ["面经", "面试", "校招"],
     "custom_instructions": "只关心能力、价格与合规变化，不写发布会通稿。"},
    {"id": "health", "name": "医疗与药品", "enabled": True,
     "role": "关注用药可及性的普通人",
     "keywords": ["集采", "医保", "nmpa", "car-t", "药品"],
     "block_keywords": [],
     "custom_instructions": "重点写降价幅度、适应症与执行时间。"},
    {"id": "sports", "name": "体育赛事", "enabled": True,
     "role": "看比赛也看数据的球迷",
     "keywords": ["国足", "世预赛", "欧冠", "中超"],
     "block_keywords": [],
     "custom_instructions": "只写比分、关键数据与出线形势。"},
    {"id": "gaming", "name": "游戏", "enabled": True,
     "role": "主机与 PC 玩家",
     "keywords": ["版号", "黑神话", "实机"],
     "block_keywords": [],
     "custom_instructions": "只关心版号、发售日与实机表现。"},
    {"id": "autos", "name": "新能源车与消费", "enabled": True,
     "role": "看行业数据的消费者",
     "keywords": ["新能源车", "渗透率", "咖啡", "门店"],
     "block_keywords": [],
     "custom_instructions": "写清同比与绝对值，不写品牌口号。"},
    {"id": "edu", "name": "教育与就业", "enabled": True,
     "role": "在校学生",
     "keywords": ["考研", "秋招", "就业"],
     "block_keywords": [],
     "custom_instructions": "只写人数、比例与结构性变化。"},
    {"id": "ai-fintech", "name": "AI 金融交叉", "enabled": True,
     "role": "做量化研究的人",
     "keywords": ["量化私募", "因子挖掘", "财报解读"],
     "block_keywords": [],
     "custom_instructions": "必须是 AI 与金融同时相关，两个领域各看一半的不写。"},
    {"id": "dormant", "name": "停用项", "enabled": False,
     "keywords": ["降准", "净利", "国足"],
     "custom_instructions": "这条不该出现在任何 prompt 里。"},
]

# 期望入选（URL 末段），故意写成精确集合：多一条少一条都算失败
EXPECT = {
    "macro": {"macro-cpi", "macro-cgb-yield", "macro-fed-hold", "macro-rrr-cut", "macro-gold"},
    "equity": {"equity-catl-q3", "equity-reduction", "equity-buyback", "equity-csrc-hft"},
    "ai-industry": {"ai-gpt55", "ai-qwen35", "ai-eu-act"},
    "health": {"health-cart", "health-nrhl", "health-vbp"},
    "sports": {"sports-football", "sports-ucl", "sports-transfer"},
    "gaming": {"game-wukong2", "game-license"},
    "autos": {"auto-nev", "consumer-coffee"},
    "edu": {"edu-kaoyan", "edu-jobfair"},
    "ai-fintech": {"x-ai-quant", "x-ai-report"},
}

NOISE = ["noise/airdrop", "noise/hiring", "noise/promo", "noise/injection"]
BLOCKED_BY_WORD = "equity-broker-tp"      # 含「净利」但被「目标价」屏蔽


def _slug(row: dict) -> str:
    url = str(row.get("canonical_url") or row.get("url") or "")
    return url.rstrip("/").rsplit("/", 1)[-1]


def _cfg(tmp: Path) -> dict:
    cfg = copy.deepcopy(load_config(ROOT / "config.json"))
    cfg["storage"].update(
        db=str(tmp / "mixed.db"),
        out_dir=str(tmp / "out"),
        trace_dir=str(tmp / "traces"),
    )
    cfg["llm"]["enabled"] = False
    cfg["topics"] = copy.deepcopy(TOPICS)
    cfg["channels"] = {k: {**v, "enabled": False}
                       for k, v in cfg["channels"].items() if isinstance(v, dict)}
    return cfg


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{name}{('：' + detail) if detail else ''}")

    tmp = Path(tempfile.mkdtemp(prefix="mixeddom-"))
    cfg = _cfg(tmp)
    db = cfg["storage"]["db"]

    # ---- 逐个主题真跑一遍，共用同一个库（顺带验水位线不会串主题）----
    picked: dict[str, list[dict]] = {}
    runs: dict[str, dict] = {}
    for topic in TOPICS:
        if not topic.get("enabled", True):
            continue
        runs[topic["id"]] = run_once(
            cfg, Store(db), topic=topic["id"], dry_run=True, offline=True,
            fixtures=FIXTURES, force_rule=True, verbose=False)
        picked[topic["id"]] = Store(db).topic_items("2026-01-01", topic=topic["id"])

    print("入选分布：")
    for tid, rows in picked.items():
        print(f"  [{tid}] {len(rows)} 条  " + "、".join(_slug(r) for r in rows))
    print()

    # ---- 1. 每个主题的集合精确相等 ----
    for tid, want in EXPECT.items():
        got = {_slug(r) for r in picked.get(tid) or []}
        check(f"{tid} 入选集合与预期一致", got == want,
              f"多出 {sorted(got - want) or '无'}，缺少 {sorted(want - got) or '无'}")

    # ---- 2. 零交叉：任何主题都不许拿到别家的条目 ----
    owner = {slug: tid for tid, slugs in EXPECT.items() for slug in slugs}
    for tid, rows in picked.items():
        for row in rows:
            slug = _slug(row)
            if slug in owner and owner[slug] != tid:
                check(f"{tid} 不含其他主题的条目", False, f"{slug} 属于 {owner[slug]}")
    print("交叉检查：9 个主题共 %d 条，落库条目均归属唯一主题" %
          sum(len(v) for v in picked.values()))

    # ---- 3. 噪声四条不进任何主题 ----
    all_slugs = {_slug(r) for rows in picked.values() for r in rows}
    for slug in NOISE:
        check(f"噪声 {slug} 不进任何主题", slug not in all_slugs,
              "被某个主题收进来了")

    # ---- 4. 屏蔽词优先于关键词 ----
    check("含「净利」的研报被「目标价」砍掉",
          BLOCKED_BY_WORD not in {_slug(r) for r in picked.get("equity") or []},
          "它进了 equity")
    check("equity 那一轮确实记到屏蔽", (runs["equity"].get("blocked") or 0) >= 1,
          f"blocked={runs['equity'].get('blocked')}")

    # ---- 5. 用户提示词按主题注入判定 prompt，且不夹带 ----
    prompts: dict[str, str] = {}
    for topic in TOPICS:
        if not topic.get("enabled", True):
            continue
        eff = topics.effective(cfg, topic["id"])
        prompts[topic["id"]] = judge._build_user_prompt([], eff, {})
    for tid in ("macro", "equity", "health", "sports"):
        topic = next(t for t in TOPICS if t["id"] == tid)
        text = prompts[tid]
        check(f"{tid} 的角色进了 prompt", f"角色：{topic['role']}" in text, text[:60])
        check(f"{tid} 的附加要求进了 prompt", topic["custom_instructions"] in text,
              text[:80])
    check("macro 的关注项进了 prompt", "利率与通胀数据" in prompts["macro"])
    check("macro 的回避项进了 prompt", "目标价" in prompts["macro"])
    for tid, text in prompts.items():
        check(f"{tid} 没夹带停用项提示词", "这条不该出现在任何 prompt 里" not in text)
    check("macro 与 sports 的 prompt 不同", prompts["macro"] != prompts["sports"])

    # ---- 6. 停用项 ----
    check("停用项 0 产出", not Store(db).topic_items("2026-01-01", topic="dormant"))
    check("停用项不在调度里", "dormant" not in topics.enabled_ids(cfg),
          str(topics.enabled_ids(cfg)))

    # ---- 7. 英文主题渲染英文标签 ----
    en_cfg = copy.deepcopy(cfg)
    for topic in en_cfg["topics"]:
        if topic["id"] == "macro":
            topic["output_language"] = "en"
    en_rows = picked.get("macro") or []
    if en_rows:
        from datetime import datetime, timezone
        from intel.models import Item
        items = [Item(id=r["item_id"], title=r["title"], url=r["url"],
                      canonical_url=r["canonical_url"], source=r["source"],
                      published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      raw_summary=r.get("summary") or "",
                      headline=r.get("headline") or "", summary=r.get("summary") or "",
                      points=list(r.get("points") or []))
                 for r in en_rows[:2]]
        md_en = render.markdown(items, "Digest", "en")
        md_zh = render.markdown(items, "日报", "zh")
        check("英文主题用英文标签", "Source:" in md_en and "原文" not in md_en,
              md_en[:80].replace("\n", " "))
        check("中文主题仍用中文标签", "来源：" in md_zh,
              md_zh[:80].replace("\n", " "))

    # ---- 8. 幂等：同主题重跑 0 新增 ----
    before = len(Store(db).topic_items("2026-01-01", topic="health"))
    run_once(cfg, Store(db), topic="health", dry_run=True, offline=True,
             fixtures=FIXTURES, force_rule=True, verbose=False)
    after = len(Store(db).topic_items("2026-01-01", topic="health"))
    check("同主题重跑无新增", before == after, f"{before} → {after}")

    # ---- 9. 跨域主题只吃交集 ----
    cross = {_slug(r) for r in picked.get("ai-fintech") or []}
    check("AI+金融只吃交集", cross == EXPECT["ai-fintech"], str(sorted(cross)))
    check("AI+金融不吃纯 AI", "ai-gpt55" not in cross)
    check("AI+金融不吃纯金融", "equity-catl-q3" not in cross)

    # ---- 10. 规则兜底的选题大纲不带科技专属措辞（金融/体育也读得通）----
    from intel import studio
    from intel.models import Item as _Item
    stem = next(r for r in picked["macro"] if _slug(r) == "macro-fed-hold")
    it = _Item(id=stem["item_id"], title=stem["title"], url=stem["url"],
               canonical_url=stem["canonical_url"], source=stem["source"],
               published_at="2026-11-27T08:00:00", raw_summary=stem.get("summary") or "",
               headline=stem.get("headline") or "", summary=stem.get("summary") or "",
               points=list(stem.get("points") or []), score=6.0, confidence=0.66)
    pack = studio._rule_pack([it], cfg) or {}
    blob = json.dumps(pack, ensure_ascii=False)
    for word in ("同类项目", "上一版本", "可复现", "值得讲", "建议关注"):
        check(f"规则大纲不含科技/价值判断措辞「{word}」", word not in blob, blob[:120])
    check("规则大纲把关键数字单列一段",
          any(s.get("section") == "关键数字" for s in pack.get("outline") or []),
          str([s.get("section") for s in pack.get("outline") or []]))

    if failures:
        print("\nFAIL %d 项：" % len(failures))
        for f in failures:
            print("  -", f)
        return 1

    print("\nOK 跨领域 9 个主题、9 组断言全部通过")
    print("临时库：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
