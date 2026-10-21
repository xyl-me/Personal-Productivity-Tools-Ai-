"""缺陷归集与指标计算。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .models import CaseResult, CaseStatus, ExecutionReport, FailureKind, TestCase, TestCaseSuite

Matcher = Callable[[TestCase, CaseResult], bool]


@dataclass(frozen=True)
class DefectSignature:
    """一个已知缺陷的"指纹"。"""

    id: str
    title: str
    category: str
    endpoint: str
    failure_kinds: frozenset[FailureKind]
    extra: Matcher | None = None

    def matches(self, case: TestCase, result: CaseResult) -> bool:
        if result.status is CaseStatus.PASSED:
            return False
        if case.endpoint != self.endpoint:
            return False
        if result.failure_kind not in self.failure_kinds:
            return False
        if self.extra is not None and not self.extra(case, result):
            return False
        return True


@dataclass
class DefectEvaluation:
    defects: Sequence[DefectSignature]
    hits: dict[str, list[str]] = field(default_factory=dict)
    missed: list[DefectSignature] = field(default_factory=list)
    unattributed: list[CaseResult] = field(default_factory=list)
    baseline_total: int = 0
    false_positives: list[CaseResult] = field(default_factory=list)
    execution_errors: list[CaseResult] = field(default_factory=list)
    total_cases: int = 0
    failed_cases: int = 0

    @property
    def total_defects(self) -> int:
        return len(self.defects)

    @property
    def detected(self) -> int:
        return sum(1 for case_ids in self.hits.values() if case_ids)

    @property
    def recall(self) -> float:
        return self.detected / self.total_defects if self.total_defects else 0.0

    @property
    def attributed_cases(self) -> int:
        return sum(len(ids) for ids in self.hits.values())

    @property
    def duplicate_hits(self) -> int:
        """超出"每个缺陷刚好被 1 条用例命中"的用例数, 衡量用例冗余度。"""
        return self.attributed_cases - self.detected

    @property
    def false_positive_rate(self) -> float:
        return len(self.false_positives) / self.baseline_total if self.baseline_total else 0.0

    def defects_hit_twice_or_more(self) -> list[tuple[str, int]]:
        return [(did, len(ids)) for did, ids in self.hits.items() if len(ids) > 1]

    def render(self) -> str:
        lines = [
            "| 缺陷 | 类别 | 命中用例 | 结果 |",
            "| --- | --- | --- | --- |",
        ]
        for defect in self.defects:
            ids = self.hits.get(defect.id, [])
            mark = ", ".join(ids) if ids else "**未命中**"
            lines.append(f"| {defect.id} {defect.title} | {defect.category} | {mark} | "
                         f"{'命中' if ids else '漏测'} |")

        lines += [
            "",
            f"- 用例: {self.total_cases} 条, 失败 {self.failed_cases} 条"
            f" (执行异常 {len(self.execution_errors)} 条已剔除)",
            f"- **缺陷召回率: {self.detected}/{self.total_defects} = {self.recall:.1%}**",
            f"- **误报率: {len(self.false_positives)}/{self.baseline_total} = "
            f"{self.false_positive_rate:.1%}**"
            " (分母为断言已知正确行为的用例)",
            f"- 未归因失败: {len(self.unattributed)} 条",
        ]
        if self.duplicate_hits > 0:
            detail = "; ".join(f"{d}×{n}" for d, n in self.defects_hit_twice_or_more())
            lines.append(f"- 重复命中: {self.duplicate_hits} 条 ({detail})")
        if self.unattributed:
            lines.append("")
            lines.append("未归因的失败 (可能是未知缺陷, 也可能是用例本身有问题):")
            lines += [f"  - {r.case_id} {r.endpoint}: {r.error}" for r in self.unattributed]
        if self.false_positives:
            lines.append("")
            lines.append("疑似误报 (对已知正确行为报失败):")
            lines += [f"  - {r.case_id} {r.endpoint}: {r.error}" for r in self.false_positives]
        return "\n".join(lines)


def attribute(
    case: TestCase | None,
    result: CaseResult,
    defects: Sequence[DefectSignature],
) -> str | None:
    """按签名顺序匹配, 返回第一个命中的缺陷编号。"""
    if case is None:
        return None
    for defect in defects:
        if defect.matches(case, result):
            return defect.id
    return None


def evaluate(
    cases: TestCaseSuite | Iterable[TestCase],
    report: ExecutionReport,
    defects: Sequence[DefectSignature],
) -> DefectEvaluation:
    case_list = list(cases.cases if isinstance(cases, TestCaseSuite) else cases)
    index = {case.id: case for case in case_list}

    evaluation = DefectEvaluation(
        defects=defects,
        hits={defect.id: [] for defect in defects},
        total_cases=report.total,
        failed_cases=report.failed,
    )

    for case in case_list:
        if case.is_baseline:
            evaluation.baseline_total += 1

    for result in report.results:
        if result.status is CaseStatus.ERROR:
            evaluation.execution_errors.append(result)
            continue
        if result.passed:
            continue

        case = index.get(result.case_id)
        defect_id = attribute(case, result, defects)
        if defect_id is not None:
            evaluation.hits[defect_id].append(result.case_id)
        elif case is not None and case.is_baseline:
            evaluation.false_positives.append(result)
        else:
            evaluation.unattributed.append(result)

    evaluation.missed = [d for d in defects if not evaluation.hits.get(d.id)]
    return evaluation
