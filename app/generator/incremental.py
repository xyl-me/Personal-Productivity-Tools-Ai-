"""增量生成: 只对"这次改过的接口"生成用例。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.executor.models import TestCase, TestCaseSuite
from app.parser.diff import SpecDiff, diff_specs

TAG_INCREMENTAL = "incremental"
TAG_ADDED = "added"
TAG_CHANGED = "changed"
TAG_BREAKING = "breaking"


@dataclass
class IncrementalPlan:
    diff: SpecDiff
    endpoints: list[str]
    removed: list[str]

    @property
    def endpoint_count(self) -> int:
        return len(self.endpoints)

    @property
    def saving(self) -> float:
        return self.diff.generation_saving


def build_plan(old_spec: object, new_spec: object) -> IncrementalPlan:
    diff = diff_specs(old_spec, new_spec)  # type: ignore[arg-type]
    return IncrementalPlan(
        diff=diff,
        endpoints=diff.impacted_uids,
        removed=[item.uid for item in diff.removed],
    )


def tag_incremental(suite: TestCaseSuite, diff: SpecDiff) -> TestCaseSuite:
    """给增量用例打标签, 让报告能一眼看出"这条是因为文档变更才生成的"。"""
    status_by_uid = {item.uid: item for item in diff.added}
    status_by_uid.update({item.uid: item for item in diff.changed})

    for case in suite.cases:
        item = status_by_uid.get(case.endpoint)
        if item is None:
            continue
        tags = list(case.tags)
        if TAG_INCREMENTAL not in tags:
            tags.append(TAG_INCREMENTAL)
        tags.append(TAG_ADDED if item.status == "added" else TAG_CHANGED)
        if item.breaking:
            tags.append(TAG_BREAKING)
        case.tags = tags
    return suite


def summarize_changes(diff: SpecDiff, *, per_endpoint: int = 3) -> list[str]:
    """每个变更接口的一句话摘要, 用于报告首部。"""
    lines: list[str] = []
    for item in diff.impacted:
        if item.status == "added":
            lines.append(f"- `{item.uid}`: 新接口, 需要从零生成用例")
            continue
        breaking = [c.describe() for c in item.breaking][:per_endpoint]
        relaxed = [c.describe() for c in item.relaxed][:per_endpoint]
        parts = []
        if breaking:
            parts.append("破坏性: " + "; ".join(breaking))
        if relaxed:
            parts.append("放宽: " + "; ".join(relaxed))
        lines.append(f"- `{item.uid}`: " + (" | ".join(parts) or "有变更"))
    return lines


def incremental_cases(suite: TestCaseSuite) -> Sequence[TestCase]:
    return [case for case in suite.cases if TAG_INCREMENTAL in case.tags]
