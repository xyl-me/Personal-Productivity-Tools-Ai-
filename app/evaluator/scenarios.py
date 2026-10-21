"""人工标注的场景清单 —— 用来算"场景召回率"。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.executor.models import TestCase
from app.parser.models import EndpointSpec

from .rules import INJECTION_MARKERS, FieldSpec, _as_number, _looks_injected, observations

MatcherMode = Literal[
    "missing",
    "empty",
    "lt",
    "lte",
    "gt",
    "gte",
    "len_gt",
    "len_lt",
    "len_between",
    "non_numeric",
    "not_in_enum",
    "injection",
    "present",
    "no_auth",
    "expect_status",
    "expect_lt",
]

DEFAULT_LABEL_PATH = Path("data") / "labels" / "scenarios.json"


class Matcher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: MatcherMode
    location: str = "body"
    field: str | None = None
    value: float | int | None = None
    value2: float | int | None = None
    enum: list[Any] | None = None

    def matches(self, case: TestCase, endpoint: EndpointSpec) -> bool:
        if self.mode == "no_auth":
            return any(code in (401, 403) for code in case.expect.statuses)
        if self.mode == "expect_status":
            return self.value is not None and float(self.value) in {float(s) for s in case.expect.statuses}
        if self.mode == "expect_lt":
            return bool(case.expect.statuses) and all(
                s < (self.value if self.value is not None else 400) for s in case.expect.statuses
            )
        if not self.field:
            return False

        spec = FieldSpec(self.location, self.field, {})
        obs = observations([case], endpoint, spec)[0]
        if self.mode == "missing":
            return not obs.present
        if self.mode == "present":
            return obs.present
        if not obs.present:
            return False

        value = obs.value
        number = _as_number(value)
        if self.mode == "empty":
            return value == ""
        if self.mode in {"lt", "lte", "gt", "gte"}:
            if number is None or self.value is None:
                return False
            bound = float(self.value)
            return {
                "lt": number < bound,
                "lte": number <= bound,
                "gt": number > bound,
                "gte": number >= bound,
            }[self.mode]
        if self.mode in {"len_gt", "len_lt", "len_between"}:
            if not isinstance(value, str) or self.value is None:
                return False
            length = len(value)
            if self.mode == "len_gt":
                return length > float(self.value)
            if self.mode == "len_lt":
                return length < float(self.value)
            upper = float(self.value2) if self.value2 is not None else float("inf")
            return float(self.value) < length <= upper
        if self.mode == "non_numeric":
            return value not in ("", None) and _as_number(value) is None
        if self.mode == "not_in_enum":
            allowed = self.enum or []
            return bool(allowed) and value not in allowed
        if self.mode == "injection":
            return isinstance(value, str) and _looks_injected(value)
        return False  # pragma: no cover - Literal 已限死


class Scenario(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    scenario: str
    matcher: Matcher
    weight: float = 1.0
    note: str | None = None


@dataclass
class ScenarioResult:
    scenario: Scenario
    covered: bool
    evidence: list[str] = field(default_factory=list)


@dataclass
class ScenarioCoverage:
    endpoint: str
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def covered(self) -> int:
        return sum(1 for item in self.results if item.covered)

    @property
    def recall(self) -> float:
        return self.covered / self.total if self.total else 0.0

    @property
    def missing(self) -> list[ScenarioResult]:
        return [item for item in self.results if not item.covered]


def load_labeled_scenarios(path: str | Path = DEFAULT_LABEL_PATH) -> dict[str, list[Scenario]]:
    """读取标注文件: ``{"endpoints": {"GET /x": [ ... ]}}``。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("endpoints", data)
    return {
        uid: [Scenario.model_validate(item) for item in items]
        for uid, items in raw.items()
    }


def evaluate_scenarios(
    endpoint: EndpointSpec, cases: Sequence[TestCase], scenarios: Sequence[Scenario]
) -> ScenarioCoverage:
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        evidence = [case.id for case in cases if scenario.matcher.matches(case, endpoint)]
        results.append(ScenarioResult(scenario=scenario, covered=bool(evidence), evidence=evidence))
    return ScenarioCoverage(endpoint=endpoint.uid, results=results)
