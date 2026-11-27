"""评测：25 条用例，覆盖正常 / 边界 / 对抗。

不跑完整 pipeline，而是逐段验证判定逻辑：
  judge   —— 单条的四闸、confidence、change_type、注入识别
  dedupe  —— 两条是否被折叠
  cluster —— 多条是否聚成一簇
  safety  —— 注入识别与渲染转义

这样评测不依赖网络、不依赖数据库，可在 CI 里跑，
也不会因为「今天 GitHub 限流」导致基线漂移。

用法：
    python tests/run_eval.py                # 规则模式
    python tests/run_eval.py --llm          # 同时跑 LLM 模式（需要 LLM_API_KEY）
    python tests/run_eval.py --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel import render                                    # noqa: E402
from intel.cluster import build_clusters                    # noqa: E402
from intel.config import load_config                        # noqa: E402
from intel import dedupe as dedupe_mod                      # noqa: E402
from intel.judge import judge_all, looks_like_injection     # noqa: E402
from intel.models import VERDICT_REJECTED                   # noqa: E402
from intel.normalize import normalize_item                  # noqa: E402


def _prep(raw: dict) -> dict:
    row = dict(raw)
    if "age_hours" in row:
        hours = float(row.pop("age_hours"))
        row["published_at"] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
            timespec="seconds")
    if "repeat" in row:
        row["raw_summary"] = (row.get("raw_summary") or "") * int(row.pop("repeat"))
    return row


def _norm(raw: dict):
    return normalize_item(_prep(raw))


def run_judge(case: dict, cfg: dict, use_llm: bool) -> list[str]:
    failures: list[str] = []
    item = _norm(case["input"])
    history = {}
    if case.get("history"):
        key = item.entities[0] if item.entities else "unknown"
        history[key] = case["history"]
    api_key = os.environ.get(cfg["llm"].get("api_key_env") or "LLM_API_KEY", "").strip()

    # 屏蔽词走的是粗排，不是 judge；这里补上，保证 j03/j04 的期望成立
    from intel.coarse import _blocked
    hit = _blocked(item, cfg["filter"])
    if hit:
        item.verdict = VERDICT_REJECTED
        item.reject_reason = f"blocked_keyword:{hit}"

    judge_all([item], cfg, history, {}, tracer=None,
              api_key=api_key, force_rule=not use_llm)

    exp = case["expect"]
    if "verdict" in exp and item.verdict != exp["verdict"]:
        failures.append(f"verdict={item.verdict}（期望 {exp['verdict']}，原因 {item.reject_reason}）")
    if "reject_contains" in exp and exp["reject_contains"] not in (item.reject_reason or ""):
        failures.append(f"reject_reason={item.reject_reason}（期望含 {exp['reject_contains']}）")
    if "min_confidence" in exp and item.confidence < exp["min_confidence"]:
        failures.append(f"confidence={item.confidence:.2f}（期望 >= {exp['min_confidence']}）")
    if "max_novelty" in exp and item.gates.get("novelty", 0) > exp["max_novelty"]:
        failures.append(f"novelty={item.gates.get('novelty')}（期望 <= {exp['max_novelty']}）")
    if "change_type" in exp and item.change_type != exp["change_type"]:
        failures.append(f"change_type={item.change_type}（期望 {exp['change_type']}）")
    if exp.get("injection") and not item.meta.get("injection_suspected"):
        failures.append("未识别出提示注入")
    return failures


def run_dedupe(case: dict, cfg: dict, use_llm: bool) -> list[str]:
    items = [_norm(x) for x in case["items"]]
    kept, audit = dedupe_mod.dedupe(items, cfg["dedupe"])
    folded = len(items) - len(kept)
    expected = case["expect"]["folded"]
    return [] if folded == expected else [f"folded={folded}（期望 {expected}）"]


def run_cluster(case: dict, cfg: dict, use_llm: bool) -> list[str]:
    items = [_norm(x) for x in case["items"]]
    clusters = build_clusters(items, cfg["cluster"])
    expected = case["expect"]["clusters"]
    return [] if len(clusters) == expected else [f"clusters={len(clusters)}（期望 {expected}）"]


def run_safety(case: dict, cfg: dict, use_llm: bool) -> list[str]:
    exp = case["expect"]
    failures: list[str] = []

    if "text" in case["input"]:
        detected = looks_like_injection(case["input"]["text"])
        if exp.get("injection") is not None and detected != exp["injection"]:
            failures.append(f"injection={detected}（期望 {exp['injection']}）")
        return failures

    item = _norm(case["input"])
    # 渲染层必须转义：标题里的 <script> 不能原样出现在输出的 HTML 里
    if exp.get("escaped"):
        html_out = render.html_digest([item], [], "xss test")
        card = json.dumps(render.feishu_card([item], "xss test"), ensure_ascii=False)
        if "<script>" in html_out or "<script>" in card:
            failures.append("HTML 未转义：输出里出现了原始 <script> 标签")
        if "onerror=alert" in html_out:
            failures.append("HTML 未转义：输出里出现了原始 onerror 属性")
    return failures


RUNNERS = {
    "judge": run_judge,
    "dedupe": run_dedupe,
    "cluster": run_cluster,
    "safety": run_safety,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="用 LLM 判定而非规则")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg = load_config(ROOT / args.config)
    cases_path = Path(__file__).parent / "cases.json"
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = data["cases"]
    pass_line = float(data["meta"]["pass_line"])

    passed = 0
    failures_by_case: dict[str, list[str]] = {}
    by_stage: dict[str, list[int]] = {}

    for case in cases:
        runner = RUNNERS.get(case["stage"])
        if runner is None:
            failures_by_case[case["id"]] = [f"未知 stage {case['stage']}"]
            continue
        try:
            failures = runner(case, cfg, args.llm)
        except Exception as exc:
            failures = [f"异常 {type(exc).__name__}: {exc}"]
        stage_stat = by_stage.setdefault(case["stage"], [0, 0])
        stage_stat[1] += 1
        if failures:
            failures_by_case[case["id"]] = failures
            print(f"  FAIL  {case['id']:<4} {case['name']}")
            for f in failures:
                print(f"        - {f}")
        else:
            passed += 1
            stage_stat[0] += 1
            if args.verbose:
                print(f"  ok    {case['id']:<4} {case['name']}")

    total = len(cases)
    rate = passed / total if total else 0.0
    print()
    print("按阶段")
    for stage, (ok, cnt) in by_stage.items():
        print(f"  {stage:<8} {ok}/{cnt}  {ok / cnt * 100:.0f}%")
    print()
    print(f"总计 {passed}/{total}  通过率 {rate * 100:.1f}%  通过线 {pass_line * 100:.0f}%")
    print("结果：" + ("PASS" if rate >= pass_line else "FAIL"))

    report = {
        "mode": "llm" if args.llm else "rule",
        "total": total,
        "passed": passed,
        "rate": round(rate, 4),
        "pass_line": pass_line,
        "by_stage": {k: {"passed": v[0], "total": v[1]} for k, v in by_stage.items()},
        "failures": failures_by_case,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    out = Path(__file__).parent / "last_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{out}")
    return 0 if rate >= pass_line else 1


if __name__ == "__main__":
    raise SystemExit(main())
