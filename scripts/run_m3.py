"""M3 冒烟脚本: 规则评分 + 场景覆盖 + (可选)LLM 评审 + 一致性报告。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from app.evaluator.agreement import (  # noqa: E402
    LabelPair,
    build_report,
    kappa_sweep,
    mae,
    spearman,
)
from app.evaluator.judge import run_judging  # noqa: E402
from app.evaluator.rules import evaluate_rules  # noqa: E402
from app.evaluator.scenarios import load_labeled_scenarios  # noqa: E402
from app.evaluator.scoring import evaluate_suite, group_by_endpoint  # noqa: E402
from app.executor.runner import load_case_suite  # noqa: E402
from app.generator.client import build_client  # noqa: E402
from app.parser import parse_spec_dict  # noqa: E402
from target_service.main import app  # noqa: E402

DEFAULT_SUITE = ROOT / "cases" / "order_service_cases.json"
QUALITY_LABELS = ROOT / "data" / "labels" / "quality.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M3: 用例质量评测与一致性校验")
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--threshold", type=float, default=80.0)
    parser.add_argument("--judge", action="store_true", help="调用 LLM 做开放式评审 (需要 Key)")
    parser.add_argument("--real", action="store_true", help="强制真实调用")
    parser.add_argument("--llm-weight", type=float, default=0.3)
    parser.add_argument(
        "--compare-with",
        default=None,
        help="与另一个用例集对比 (例如手写基线 cases/order_service_cases.json)",
    )
    return parser.parse_args(argv)


def print_comparison(baseline, candidate, *, baseline_name: str, candidate_name: str) -> None:
    """两份用例集的逐接口对照。规则与场景清单完全相同, 差异只来自用例本身。"""
    left = {item.endpoint: item for item in baseline.endpoints}
    right = {item.endpoint: item for item in candidate.endpoints}
    order = [item.endpoint for item in baseline.endpoints]
    order += [item.endpoint for item in candidate.endpoints if item.endpoint not in left]

    print(f"## 对照: {baseline_name} vs {candidate_name}\n")
    print("| 接口 | 基线 规则分 | 对比 规则分 | 差值 | 基线 场景 | 对比 场景 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for uid in order:
        b, c = left.get(uid), right.get(uid)
        b_score = f"{b.rule.rule_score:.1f}" if b else "-"
        c_score = f"{c.rule.rule_score:.1f}" if c else "-"
        delta = f"{(c.rule.rule_score - b.rule.rule_score):+.1f}" if (b and c) else "-"
        b_sc = f"{b.scenario.covered}/{b.scenario.total}" if b and b.scenario else "-"
        c_sc = f"{c.scenario.covered}/{c.scenario.total}" if c and c.scenario else "-"
        print(f"| `{uid}` | {b_score} | {c_score} | {delta} | {b_sc} | {c_sc} |")

    print(
        f"\n- 平均规则分: 基线 {baseline.mean_score:.1f} → 对比 {candidate.mean_score:.1f}"
        f" ({candidate.mean_score - baseline.mean_score:+.1f})"
    )
    print(f"- 用例数: 基线 {baseline.total_cases} → 对比 {candidate.total_cases}")
    print(
        f"- 通过阈值: 基线 {len(baseline.passed)}/{len(baseline.endpoints)} → "
        f"对比 {len(candidate.passed)}/{len(candidate.endpoints)}"
    )
    if baseline.scenario_recall is not None and candidate.scenario_recall is not None:
        print(
            f"- 场景召回: 基线 {baseline.scenario_recall:.1%} → "
            f"对比 {candidate.scenario_recall:.1%}"
        )


def print_details(score) -> None:
    print("## 逐接口缺口明细\n")
    for item in score.endpoints:
        misses = [f"规则·{o.title} ({o.note})" for o in item.rule.missed]
        if item.scenario is not None:
            misses += [f"场景·{s.scenario.scenario}" for s in item.scenario.missing]
        if not misses:
            print(f"- `{item.endpoint}`: 无缺口 (规则分 {item.rule.rule_score:.1f})")
            continue
        print(f"- `{item.endpoint}` 规则分 {item.rule.rule_score:.1f} / 最终 {item.final_score:.1f}")
        for line in misses[:8]:
            print(f"    - {line}")
    print()


def build_label_pairs(spec, score, labels_path: Path, suite_name: str) -> tuple[list[LabelPair], list[str]]:
    data = json.loads(labels_path.read_text(encoding="utf-8"))
    threshold = float(data.get("threshold", 80))
    by_endpoint = {item.endpoint: item for item in score.endpoints}
    pairs: list[LabelPair] = []
    skipped: list[str] = []
    for label in data.get("labels", []):
        if label.get("suite") != suite_name:
            continue
        endpoint = label["endpoint"]
        item = by_endpoint.get(endpoint)
        if item is None:
            skipped.append(endpoint)
            continue
        pairs.append(
            LabelPair(
                key=endpoint,
                rule_score=item.rule.rule_score,
                human_score=float(label["human_score"]),
                rule_pass=item.rule.rule_score >= threshold,
                human_pass=bool(label["human_pass"]),
            )
        )
    notes = [data.get("note", "")] if data.get("note") else []
    if skipped:
        notes.append("以下标注接口在该用例集里没有用例, 已跳过: " + ", ".join(skipped))
    return pairs, notes


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    spec = parse_spec_dict(app.openapi())
    suite = load_case_suite(args.suite)
    grouped = group_by_endpoint(suite.cases)
    print(f"用例集: {suite.suite} ({len(suite)} 条, 覆盖 {len(grouped)} 个接口)\n")

    scenarios = load_labeled_scenarios()

    verdicts: dict = {}
    judge_errors: dict = {}
    if args.judge:
        client = build_client(mode="real" if args.real else "auto")
        if client.mode != "real":
            print("未配置 DEEPSEEK_API_KEY, 跳过 LLM 评审 (最终分 = 规则分), 不编造模型分。\n")
        else:
            print(f"LLM 评审中 (模型 {client.model}) ...")
            records = run_judging(spec, grouped, client)
            for uid, record in records.items():
                if record.verdict is not None:
                    verdicts[uid] = record.verdict
                else:
                    judge_errors[uid] = record.error
                print(f"  - {uid}: " + (f"{record.verdict.score} 分" if record.verdict else f"失败 ({record.error})"))
            print()

    score = evaluate_suite(
        spec,
        suite,
        scenarios=scenarios,
        verdicts=verdicts,
        judge_errors=judge_errors,
        threshold=args.threshold,
        llm_weight=args.llm_weight,
    )

    print("## 质量评分\n")
    print(score.render())
    print()
    print_details(score)

    if args.compare_with:
        baseline_suite = load_case_suite(args.compare_with)
        baseline_score = evaluate_suite(
            spec,
            baseline_suite,
            scenarios=scenarios,
            threshold=args.threshold,
            llm_weight=args.llm_weight,
        )
        print()
        print_comparison(
            baseline_score,
            score,
            baseline_name=baseline_suite.suite,
            candidate_name=suite.suite,
        )
        print()

    pairs, notes = build_label_pairs(spec, score, QUALITY_LABELS, suite.suite)
    if pairs:
        print("## 一致性: 规则分 vs 人工标注\n")
        print(build_report(pairs, notes=notes).render())
        print()
        print("### 通过阈值扫描 (κ 随阈值变化)\n")
        print("| 阈值 | κ | 分流一致率 |")
        print("| --- | --- | --- |")
        for threshold, kappa, agree in kappa_sweep(pairs):
            mark = " ← 当前" if abs(threshold - args.threshold) < 1e-6 else ""
            print(f"| {threshold:.0f} | {kappa:.3f} | {agree:.1%}{mark} |")
        print()
    else:
        print("## 一致性: 规则分 vs 人工标注\n")
        print(f"该用例集没有对应的人工标注 (标注文件只覆盖 {suite.suite}), 已跳过。\n")

    with_scenario = [item for item in score.endpoints if item.scenario is not None]
    if len(with_scenario) >= 2:
        rule_values = [item.rule.rule_score for item in with_scenario]
        scenario_values = [100.0 * item.scenario.recall for item in with_scenario]
        print("## 互检: 规则分 vs 人工标注场景覆盖率\n")
        print("两条独立的度量 —— 规则是通用维度自动推断, 场景清单是业务维度人工编写。")
        print(f"- Spearman ρ: {spearman(rule_values, scenario_values)}")
        print(f"- MAE: {mae(rule_values, scenario_values)}")
        print()
        print("| 接口 | 规则分 | 场景覆盖分 |")
        print("| --- | --- | --- |")
        for item in with_scenario:
            print(
                f"| `{item.endpoint}` | {item.rule.rule_score:.1f} | "
                f"{100.0 * item.scenario.recall:.1f} |"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
