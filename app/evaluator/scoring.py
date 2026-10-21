"""把三路信号合成一个可执行的质量结论。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from app.executor.models import TestCase, TestCaseSuite
from app.parser.models import EndpointSpec

from .judge import JudgeVerdict
from .rules import RuleEvaluation, evaluate_rules
from .scenarios import Scenario, ScenarioCoverage, evaluate_scenarios

DEFAULT_THRESHOLD = 80.0
DEFAULT_LLM_WEIGHT = 0.3

VERDICT_PASS = "AI已验证"
VERDICT_REVIEW = "需人工复核"


def group_by_endpoint(cases: Iterable[TestCase]) -> dict[str, list[TestCase]]:
    grouped: dict[str, list[TestCase]] = {}
    for case in cases:
        grouped.setdefault(case.endpoint, []).append(case)
    return grouped


@dataclass
class EndpointScore:
    endpoint: str
    case_count: int
    rule: RuleEvaluation
    scenario: ScenarioCoverage | None = None
    llm: JudgeVerdict | None = None
    llm_error: str | None = None
    llm_weight: float = DEFAULT_LLM_WEIGHT
    final_score: float = 0.0
    verdict: str = VERDICT_REVIEW
    review_reasons: list[str] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return self.verdict == VERDICT_PASS


@dataclass
class SuiteScore:
    endpoints: list[EndpointScore]
    threshold: float = DEFAULT_THRESHOLD

    @property
    def total_cases(self) -> int:
        return sum(item.case_count for item in self.endpoints)

    @property
    def mean_score(self) -> float:
        return (
            sum(item.final_score for item in self.endpoints) / len(self.endpoints)
            if self.endpoints
            else 0.0
        )

    @property
    def passed(self) -> list[EndpointScore]:
        return [item for item in self.endpoints if item.passes]

    @property
    def review(self) -> list[EndpointScore]:
        return [item for item in self.endpoints if not item.passes]

    @property
    def pass_rate(self) -> float:
        return len(self.passed) / len(self.endpoints) if self.endpoints else 0.0

    @property
    def scenario_recall(self) -> float | None:
        covers = [item.scenario for item in self.endpoints if item.scenario is not None]
        if not covers:
            return None
        total = sum(cover.total for cover in covers)
        covered = sum(cover.covered for cover in covers)
        return covered / total if total else 0.0

    def render(self) -> str:
        lines = [
            "| 接口 | 用例 | 覆盖分 | 用例质量 | 规则分 | 场景召回 | LLM | 最终分 | 结论 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in self.endpoints:
            scenario = f"{item.scenario.covered}/{item.scenario.total}" if item.scenario else "-"
            llm = f"{item.llm.score}" if item.llm else ("失败" if item.llm_error else "-")
            lines.append(
                f"| `{item.endpoint}` | {item.case_count} | {item.rule.coverage_score:.0f} | "
                f"{item.rule.case_quality_score:.0f} | {item.rule.rule_score:.1f} | {scenario} | "
                f"{llm} | **{item.final_score:.1f}** | {item.verdict} |"
            )

        lines += [
            "",
            f"- 用例总数 {self.total_cases}, 平均最终分 {self.mean_score:.1f}, "
            f"阈值 {self.threshold:.0f}, 通过 {len(self.passed)}/{len(self.endpoints)} "
            f"({self.pass_rate:.0%})",
        ]
        recall = self.scenario_recall
        if recall is not None:
            lines.append(f"- 人工标注场景召回率: {recall:.1%}")
        if self.review:
            lines.append("")
            lines.append("需人工复核的原因:")
            for item in self.review:
                reasons = item.review_reasons or ["综合分低于阈值"]
                lines.append(f"- `{item.endpoint}`: " + "; ".join(reasons))
        return "\n".join(lines)


def evaluate_endpoint(
    endpoint: EndpointSpec,
    cases: Sequence[TestCase],
    *,
    scenarios: Sequence[Scenario] | None = None,
    llm: JudgeVerdict | None = None,
    llm_error: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    llm_weight: float = DEFAULT_LLM_WEIGHT,
) -> EndpointScore:
    rule = evaluate_rules(endpoint, cases)
    scenario = evaluate_scenarios(endpoint, cases, scenarios) if scenarios else None

    if llm is not None:
        final = (1.0 - llm_weight) * rule.rule_score + llm_weight * float(llm.score)
    else:
        final = rule.rule_score

    reasons: list[str] = []
    for outcome in rule.missed:
        reasons.append(f"未覆盖: {outcome.title}")
    if scenario is not None:
        for item in scenario.missing:
            reasons.append(f"未覆盖标注场景: {item.scenario.scenario}")
    if llm is not None:
        for issue in llm.high_issues[:3]:
            location = f"[{issue.case_id}] " if issue.case_id else ""
            reasons.append(f"评审高危: {location}{issue.problem}")

    return EndpointScore(
        endpoint=endpoint.uid,
        case_count=len(cases),
        rule=rule,
        scenario=scenario,
        llm=llm,
        llm_error=llm_error,
        llm_weight=llm_weight,
        final_score=round(final, 1),
        verdict=VERDICT_PASS if final >= threshold else VERDICT_REVIEW,
        review_reasons=reasons,
    )


def evaluate_suite(
    spec: Any,
    suite: TestCaseSuite | Sequence[TestCase],
    *,
    scenarios: dict[str, Sequence[Scenario]] | None = None,
    verdicts: dict[str, JudgeVerdict | None] | None = None,
    judge_errors: dict[str, str | None] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    llm_weight: float = DEFAULT_LLM_WEIGHT,
) -> SuiteScore:
    cases = list(suite.cases if isinstance(suite, TestCaseSuite) else suite)
    grouped = group_by_endpoint(cases)

    scores: list[EndpointScore] = []
    for endpoint in spec.endpoints:
        endpoint_cases = grouped.get(endpoint.uid, [])
        if not endpoint_cases:
            continue
        scores.append(
            evaluate_endpoint(
                endpoint,
                endpoint_cases,
                scenarios=(scenarios or {}).get(endpoint.uid),
                llm=(verdicts or {}).get(endpoint.uid),
                llm_error=(judge_errors or {}).get(endpoint.uid),
                threshold=threshold,
                llm_weight=llm_weight,
            )
        )
    return SuiteScore(endpoints=scores, threshold=threshold)


def render_scores(score: SuiteScore) -> str:
    return score.render()
