"""变更点覆盖检查: 增量用例有没有真的碰到"这次改的东西"。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.executor.models import TestCase, TestCaseSuite
from app.parser.diff import SpecDiff

from .rules import FieldSpec, observations


@dataclass(frozen=True)
class ChangeMiss:
    endpoint: str
    location: str
    field_name: str
    kind: str
    severity: str

    def describe(self) -> str:
        return f"{self.endpoint} -> {self.location}.{self.field_name} ({self.kind}, {self.severity})"


@dataclass
class ChangeCoverage:
    total: int = 0
    covered: int = 0
    misses: list[ChangeMiss] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.covered / self.total if self.total else 1.0

    @property
    def breaking_misses(self) -> list[ChangeMiss]:
        return [miss for miss in self.misses if miss.severity == "breaking"]

    def render(self) -> str:
        lines = [f"变更点覆盖: {self.covered}/{self.total} = {self.ratio:.1%}"]
        if self.misses:
            lines.append("")
            lines.append("未被覆盖的变更点 (增量生成的质量门禁):")
            lines.extend(f"- {miss.describe()}" for miss in self.misses)
            if self.breaking_misses:
                lines.append("")
                lines.append(
                    f"其中 {len(self.breaking_misses)} 个是破坏性变更 —— 这些没测到, "
                    "等于这次迭代的回归风险完全没有被覆盖。"
                )
        return "\n".join(lines)


def evaluate_change_coverage(suite: TestCaseSuite, diff: SpecDiff, spec: object) -> ChangeCoverage:
    """``spec`` 是新版文档 (需要它来还原路径参数, 例如 ``/orders/{id}`` -> 实际 URL)。"""
    index = spec.endpoint_index() if hasattr(spec, "endpoint_index") else {}
    cases_by_endpoint: dict[str, list[TestCase]] = {}
    for case in suite.cases:
        cases_by_endpoint.setdefault(case.endpoint, []).append(case)

    coverage = ChangeCoverage()
    for item in diff.impacted:
        cases = cases_by_endpoint.get(item.uid, [])
        endpoint = index.get(item.uid)

        if item.status == "added":
            coverage.total += 1
            if cases:
                coverage.covered += 1
            else:
                coverage.misses.append(
                    ChangeMiss(item.uid, "endpoint", item.uid, "added", "breaking")
                )
            continue

        for change in item.changes:
            if change.location == "response":
                continue
            coverage.total += 1
            if endpoint is None:
                coverage.misses.append(
                    ChangeMiss(
                        item.uid, change.location, change.field_name, change.kind, change.severity
                    )
                )
                continue
            field_spec = FieldSpec(change.location, change.field_name, {})
            touched = any(
                observation.present
                for case in cases
                for observation in observations([case], endpoint, field_spec)
            )
            if touched:
                coverage.covered += 1
            else:
                coverage.misses.append(
                    ChangeMiss(
                        item.uid, change.location, change.field_name, change.kind, change.severity
                    )
                )
    return coverage
