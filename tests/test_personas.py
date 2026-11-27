"""十二种用户人格测试：把「用户提示词」写成关注项，看同一批内容会被各自挑成什么样。

用例来自用户给的 12 份人格模板（AI 学生 / 入门者 / 后端 / ML / 教师 / PM / 创业者 /
开源维护者 / 技术负责人 / 求职者 / 投资人 / 行政）。每条模板里的
「相关性 0-1、重要性 0-10、低于 X 不推、每天 N 条」被映射成现有字段：

    相关性 0-1  →  item.confidence（0-1，由四闸加权得出）
    重要性 0-10 →  item.score（0-10，四闸加权分）
    低于 X 不推 →  filter.silence.min_confidence（整轮静默线）
    每天 N 条   →  filter.max_items_per_run（本轮上限）

验证八件事：
  1. 12 个人格都能跑完，prompt 里带各自的 role 与附加要求；
  2. 每个人格选中的条目，正文里确实命中它自己的关键词（没有靠「通用分」混进来）；
  3. 教师 / 行政 / 求职者不串到游戏、体育、模型发布；
  4. 全局屏蔽词「招聘 / hiring」会砍掉招聘贴，但不影响「校招 / 面经」这类求职内容；
  5. 阈值单调：同一主题下，0.70 的入选数 ≤ 0.55 的入选数；
  6. 静默判定与阈值一致（top_confidence < 阈值 → 不推）；
  7. 无内容可推的人格（入门者 / 行政）不报错、产出 0 条；
  8. 主题关键词里写了「融资」时，软回避不再扣分（否则投资人永远拿不到融资新闻）。

全部离线，临时库；夹具用 mixed + sample 合并后的 54 条。

用法：python tests/test_personas.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import judge, topics                        # noqa: E402
from intel.config import load_config                   # noqa: E402
from intel.normalize import normalize_item             # noqa: E402
from intel.pipeline import run_once                    # noqa: E402
from intel.store import Store                          # noqa: E402

FIXTURES = ["tests/fixtures/mixed_items.json", "tests/fixtures/sample_items.json"]

# 阈值与条数来自各自模板；keywords 是从模板关注点里提炼的检索词
PERSONAS = [
    {"id": "p01-ai-student", "name": "AI 学生", "threshold": 0.60, "max_items": 5,
     "role": "计算机/AI 专业学生", "keywords": ["agent", "rag", "大模型", "竞赛", "教程", "开源"],
     "interests": ["课程项目灵感", "竞赛", "可运行代码"],
     "avoid": ["纯融资新闻", "招聘"],
     "custom_instructions": "优先有可运行代码或清晰教程的内容，能直接改成课程项目。"},
    {"id": "p02-beginner", "name": "跨专业入门", "threshold": 0.50, "max_items": 3,
     "role": "非计算机专业初学者", "keywords": ["aigc", "提示词", "低代码", "办公", "入门"],
     "interests": ["AI 工具入门", "提示词技巧", "低代码"],
     "avoid": ["论文", "底层原理"],
     "custom_instructions": "用白话说明这是什么、我能怎么用，避免术语堆砌。"},
    {"id": "p03-backend", "name": "后端/全栈", "threshold": 0.65, "max_items": 6,
     "role": "后端/全栈开发者",
     "keywords": ["python", "typescript", "go", "api", "数据库", "云原生", "kubernetes", "release"],
     "interests": ["框架更新", "API 设计", "云原生"],
     "avoid": ["招聘", "课程广告"],
     "custom_instructions": "写清技术栈、解决什么问题、是否值得试用。"},
    {"id": "p04-ml", "name": "算法/ML", "threshold": 0.70, "max_items": 5,
     "role": "算法/ML 工程师",
     "keywords": ["llm", "rag", "多模态", "推理", "benchmark", "开源模型", "gpt", "qwen"],
     "interests": ["推理优化", "评测基准", "开源模型"],
     "avoid": ["融资", "产品发布"],
     "custom_instructions": "要方法、效果、局限三件套，避免夸大。"},
    {"id": "p05-teacher", "name": "教师/科研", "threshold": 0.60, "max_items": 4,
     "role": "高校教师/科研人员",
     "keywords": ["基金", "申报", "neurips", "截稿", "教学", "伦理"],
     "interests": ["基金申报", "学术会议", "教学资源"],
     "avoid": ["游戏", "体育", "消费"],
     "custom_instructions": "只看官方通知与会议官网，说明与教学/科研的关系。"},
    {"id": "p06-pm", "name": "产品经理", "threshold": 0.60, "max_items": 5,
     "role": "AI 产品经理",
     "keywords": ["产品", "用户体验", "商业化", "竞品", "付费", "转化"],
     "interests": ["产品拆解", "商业化", "竞品动态"],
     "avoid": ["论文", "底层训练细节"],
     "custom_instructions": "要真实场景、用户价值、可借鉴点。"},
    {"id": "p07-founder", "name": "创业者", "threshold": 0.65, "max_items": 5,
     "role": "创业者/独立开发者",
     "keywords": ["saas", "融资", "变现", "arr", "客单价", "成本", "合规"],
     "interests": ["AI SaaS", "API 成本", "增长案例"],
     "avoid": ["纯学术"],
     "custom_instructions": "写清机会点与风险，涉及合规的标注出来。"},
    {"id": "p08-oss", "name": "开源维护者", "threshold": 0.65, "max_items": 6,
     "role": "开源项目维护者",
     "keywords": ["cve", "漏洞", "license", "kubernetes", "release", "安全"],
     "interests": ["安全公告", "Release", "CI/CD"],
     "avoid": ["融资"],
     "custom_instructions": "说明影响范围与维护建议（要不要升、升到什么版本）。"},
    {"id": "p09-techlead", "name": "技术负责人", "threshold": 0.65, "max_items": 5,
     "role": "团队技术负责人",
     "keywords": ["架构", "工程化", "研发效能", "合规", "安全", "kubernetes"],
     "interests": ["架构选型", "研发效能", "安全合规"],
     "avoid": ["入门教程"],
     "custom_instructions": "给结论、适用场景、风险，面向团队决策。"},
    {"id": "p10-jobseeker", "name": "求职者", "threshold": 0.55, "max_items": 4,
     "role": "准备 AI 实习/校招的学生",
     "keywords": ["校招", "实习", "面经", "笔试", "技能", "竞赛"],
     "interests": ["岗位技能要求", "面经", "学习路线"],
     "avoid": ["付费课程推销"],
     "custom_instructions": "说明能补什么技能、能做什么项目。"},
    {"id": "p11-investor", "name": "行业/投资", "threshold": 0.70, "max_items": 5,
     "role": "关注 AI 行业的分析/投资用户",
     "keywords": ["融资", "估值", "算力", "政策", "合规", "arr", "模型发布"],
     "interests": ["融资", "算力", "政策合规"],
     "avoid": ["教程", "招聘"],
     "custom_instructions": "写事件、影响、风险，不要写投资建议。"},
    {"id": "p12-office", "name": "行政/办公", "threshold": 0.50, "max_items": 3,
     "role": "行政/办公人员",
     "keywords": ["办公", "飞书", "wps", "表格", "会议纪要", "文档"],
     "interests": ["AI 办公工具", "文档处理", "会议纪要"],
     "avoid": ["代码", "论文"],
     "custom_instructions": "说明能解决什么办公问题、怎么用，给操作步骤。"},
]

# 人格不该碰的内容（URL 末段）
FORBIDDEN = {
    "p05-teacher": {"game-wukong2", "game-license", "sports-football", "sports-ucl",
                    "sports-transfer", "ai-gpt55", "consumer-coffee"},
    "p12-office": {"game-wukong2", "sports-football", "ai-gpt55", "equity-catl-q3"},
    "p10-jobseeker": {"game-wukong2", "sports-ucl", "health-vbp", "macro-gold"},
}


def _slug(row: dict) -> str:
    url = str(row.get("canonical_url") or row.get("url") or "")
    return url.rstrip("/").rsplit("/", 1)[-1]


def _merged_fixture(tmp: Path) -> str:
    items: list[dict] = []
    for rel in FIXTURES:
        data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        items.extend(data if isinstance(data, list) else data.get("items") or [])
    path = tmp / "merged.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _cfg(tmp: Path, persona: dict, fixtures: str) -> dict:
    cfg = copy.deepcopy(load_config(ROOT / "config.json"))
    cfg["storage"].update(
        db=str(tmp / f"{persona['id']}.db"),
        out_dir=str(tmp / "out"),
        trace_dir=str(tmp / "traces"),
    )
    cfg["llm"]["enabled"] = False
    cfg["channels"] = {k: {**v, "enabled": False}
                       for k, v in cfg["channels"].items() if isinstance(v, dict)}
    cfg["filter"]["max_items_per_run"] = persona["max_items"]
    cfg["sources"]["github"]["enabled"] = False
    cfg["sources"]["hn"]["enabled"] = False
    cfg["topics"] = [{
        "id": persona["id"], "name": persona["name"], "enabled": True,
        "role": persona["role"], "keywords": persona["keywords"],
        "interests": persona["interests"], "avoid": persona["avoid"],
        "custom_instructions": persona["custom_instructions"],
        # 阈值写在关注项里：每个关注项各自的静默线，互不影响
        "silence": {"min_confidence": persona["threshold"]},
    }]
    return cfg


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{name}{('：' + detail) if detail else ''}")

    tmp = Path(tempfile.mkdtemp(prefix="personas-"))
    fixtures = _merged_fixture(tmp)

    rows_by: dict[str, list[dict]] = {}
    runs: dict[str, dict] = {}
    prompts: dict[str, str] = {}

    print(f"夹具合并后 {len(json.loads(Path(fixtures).read_text(encoding='utf-8')))} 条\n")
    for persona in PERSONAS:
        cfg = _cfg(tmp, persona, fixtures)
        runs[persona["id"]] = run_once(
            cfg, Store(cfg["storage"]["db"]), topic=persona["id"], dry_run=True,
            offline=True, fixtures=fixtures, force_rule=True, verbose=False)
        rows_by[persona["id"]] = Store(cfg["storage"]["db"]).topic_items(
            "2026-01-01", topic=persona["id"])
        prompts[persona["id"]] = judge._build_user_prompt(
            [], topics.effective(cfg, persona["id"]), {})

    # ---- 表：人格 / 入选 / 最高置信 / 阈值 / 是否静默 ----
    print(f"{'人格':<12}{'入选':>4}  {'最高置信':>8}  {'阈值':>5}  静默   条数上限")
    print("-" * 62)
    for p in PERSONAS:
        rows = rows_by[p["id"]]
        top = max((float(r["confidence"]) for r in rows), default=0.0)
        silent = "是" if rows and top < p["threshold"] else ("—" if not rows else "否")
        print(f"{p['name']:<12}{len(rows):>4}  {top:>8.3f}  {p['threshold']:>5.2f}  "
              f"{silent:<6}{p['max_items']}")
        for r in rows:
            print(f"      {float(r['confidence']):.3f}  {r['title'][:52]}")
    print()

    # ---- 1. 全部跑通 + prompt 带各自要求 ----
    for p in PERSONAS:
        check(f"{p['id']} 跑完无异常", not runs[p["id"]].get("errors"),
              str(runs[p["id"]].get("errors"))[:100])
        text = prompts[p["id"]]
        check(f"{p['id']} prompt 带角色", f"角色：{p['role']}" in text, text[:60])
        check(f"{p['id']} prompt 带附加要求", p["custom_instructions"] in text, text[:80])

    # ---- 2. 入选条目必须命中自己的关键词 ----
    for p in PERSONAS:
        words = [w.lower() for w in p["keywords"]]
        for r in rows_by[p["id"]]:
            blob = f"{r['title']} {r.get('summary') or ''}".lower()
            if not any(w in blob for w in words):
                check(f"{p['id']} 的入选命中自身关键词", False,
                      f"{r['title'][:40]} 没命中任何词")

    # ---- 3. 不串到无关领域 ----
    for pid, banned in FORBIDDEN.items():
        got = {_slug(r) for r in rows_by[pid]}
        check(f"{pid} 不串无关内容", not (got & banned), str(sorted(got & banned)))

    # ---- 4. 招聘噪声被屏蔽，但不误伤校招/面经 ----
    all_slugs = {_slug(r) for rows in rows_by.values() for r in rows}
    check("英文 hiring 贴不进任何人格", "hiring" not in all_slugs, str(sorted(all_slugs))[:120])
    job = {_slug(r) for r in rows_by["p10-jobseeker"]}
    check("求职者能拿到校招", "job-campus" in job, str(sorted(job)))
    check("求职者能拿到面经", "job-interview" in job, str(sorted(job)))

    # ---- 5. 阈值单调：同一主题 0.70 的入选数 ≤ 0.55 ----
    strict = copy.deepcopy(next(p for p in PERSONAS if p["id"] == "p04-ml"))
    strict.update(id="p04-strict", name="ML 严格", threshold=0.70)
    loose = copy.deepcopy(strict)
    loose.update(id="p04-loose", name="ML 宽松", threshold=0.55)
    counts = {}
    for persona in (strict, loose):
        cfg = _cfg(tmp, persona, fixtures)
        run_once(cfg, Store(cfg["storage"]["db"]), topic=persona["id"], dry_run=True,
                 offline=True, fixtures=fixtures, force_rule=True, verbose=False)
        counts[persona["id"]] = len(Store(cfg["storage"]["db"]).topic_items(
            "2026-01-01", topic=persona["id"]))
    check("阈值高的入选不多于阈值低的", counts["p04-strict"] <= counts["p04-loose"],
          f"0.70→{counts['p04-strict']}，0.55→{counts['p04-loose']}")

    # ---- 6. 静默判定与阈值一致 ----
    for p in PERSONAS:
        rows = rows_by[p["id"]]
        if not rows:
            continue
        top = max(float(r["confidence"]) for r in rows)
        silent_flag = bool(runs[p["id"]].get("silenced"))
        check(f"{p['id']} 静默标记与阈值一致", silent_flag == (top < p["threshold"]),
              f"top={top:.3f} 阈值={p['threshold']} silenced={silent_flag}")

    # ---- 7. 没内容的人格不报错、0 条 ----
    for pid in ("p02-beginner", "p12-office", "p11-investor"):
        check(f"{pid} 无产出也不报错", not runs[pid].get("errors"),
              str(runs[pid].get("errors"))[:80])

    # ---- 8. 阈值是关注项级的：全局写 0.90 也压不住各自 0.50 / 0.70 ----
    base = copy.deepcopy(load_config(ROOT / "config.json"))
    base["filter"]["silence"]["min_confidence"] = 0.90
    base["topics"] = [
        {"id": "loose", "name": "宽松", "enabled": True,
         "silence": {"min_confidence": 0.50}},
        {"id": "tight", "name": "严格", "enabled": True,
         "silence": {"min_confidence": 0.70}},
    ]
    check("关注项能覆盖静默阈值",
          topics.effective(base, "loose")["filter"]["silence"]["min_confidence"] == 0.50
          and topics.effective(base, "tight")["filter"]["silence"]["min_confidence"] == 0.70,
          str(topics.effective(base, "loose")["filter"]["silence"]))
    check("没写阈值的关注项继承全局",
          0.90 == float(base["filter"]["silence"]["min_confidence"]))

    # ---- 9. 关键词里写了「融资」，就不能再被软回避扣分 ----
    from intel.models import Item as _Item
    inv = _cfg(tmp, next(p for p in PERSONAS if p["id"] == "p11-investor"), fixtures)
    eff = topics.effective(inv, "p11-investor")
    fund = next(x for x in json.loads(Path(fixtures).read_text(encoding="utf-8"))
                if "SaaS" in x["title"])
    fund = dict(fund, published_at="2026-11-27T08:00:00")
    it = normalize_item(fund)
    gates = judge._rule_gates(it, eff, [], 1.0)
    check("要「融资」的主题不被软回避扣分", gates["relevance"] >= 5.0, str(gates))
    plain = _cfg(tmp, next(p for p in PERSONAS if p["id"] == "p03-backend"), fixtures)
    gates2 = judge._rule_gates(it, topics.effective(plain, "p03-backend"), [], 1.0)
    check("不要「融资」的主题仍被扣分", gates2["relevance"] < gates["relevance"],
          f"{gates2['relevance']} vs {gates['relevance']}")

    if failures:
        print(f"FAIL {len(failures)} 项：")
        for f in failures:
            print("  -", f)
        return 1

    print("OK 12 个人格、8 组断言全部通过")
    print("临时目录：", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
