"""执行层数据模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CaseStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class FailureKind(str, Enum):
    """失败类型。分类本身就是有价值的信号, 也是缺陷归因的主要依据。"""

    SERVER_ERROR = "server_error"
    UNEXPECTED_SUCCESS = "unexpected_success"
    STATUS_MISMATCH = "status_mismatch"
    CONTENT_MISMATCH = "content_mismatch"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"


class Expectation(BaseModel):
    """一条用例断言什么。全部为可选, 但至少要有一个, 否则用例没有意义。"""

    model_config = ConfigDict(extra="forbid")

    status: int | list[int] | None = None
    json_contains: dict[str, Any] | None = None
    json_length: int | None = None
    body_contains: list[str] = Field(default_factory=list)

    @property
    def statuses(self) -> list[int]:
        if self.status is None:
            return []
        if isinstance(self.status, int):
            return [self.status]
        return list(self.status)

    @property
    def is_negative(self) -> bool:
        """是否断言"应当被拒绝" —— 用于判定 UNEXPECTED_SUCCESS。"""
        statuses = self.statuses
        return bool(statuses) and all(code >= 400 for code in statuses)

    def describe(self) -> str:
        bits: list[str] = []
        if self.statuses:
            bits.append("status=" + "/".join(str(code) for code in self.statuses))
        if self.json_contains:
            bits.append(f"json⊇{sorted(self.json_contains)}")
        if self.json_length is not None:
            bits.append(f"length={self.json_length}")
        if self.body_contains:
            bits.append(f"body⊇{self.body_contains}")
        return "; ".join(bits) or "(无断言)"


class TestCase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    endpoint: str
    method: str
    path: str
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    expect: Expectation = Field(default_factory=Expectation)
    tags: list[str] = Field(default_factory=list)
    description: str | None = None

    @field_validator("method")
    @classmethod
    def _upper_method(cls, value: str) -> str:
        return value.upper()

    @field_validator("path")
    @classmethod
    def _leading_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"

    @property
    def is_control(self) -> bool:
        return "control" in self.tags

    @property
    def is_positive(self) -> bool:
        return "positive" in self.tags

    @property
    def is_baseline(self) -> bool:
        """断言的是"已知正确行为", 它失败就意味着误报或未知缺陷。"""
        return self.is_control or self.is_positive

    def preview(self) -> str:
        text = self.method + " " + self.path
        if self.query:
            text += "?" + "&".join(f"{k}={v}" for k, v in self.query.items())
        return text


class TestCaseSuite(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suite: str = "default"
    target: str | None = None
    cases: list[TestCase] = Field(default_factory=list)

    @property
    def by_id(self) -> dict[str, TestCase]:
        return {case.id: case for case in self.cases}

    def __len__(self) -> int:
        return len(self.cases)


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    name: str
    endpoint: str
    method: str
    path: str
    status: CaseStatus
    failure_kind: FailureKind | None = None
    http_status: int | None = None
    duration_ms: float = 0.0
    error: str | None = None
    response_snippet: str = ""
    expected: str = ""

    @property
    def passed(self) -> bool:
        return self.status is CaseStatus.PASSED

    @property
    def failed(self) -> bool:
        return self.status is not CaseStatus.PASSED


class ExecutionReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suite: str = ""
    started_at: str = ""
    duration_ms: float = 0.0
    results: list[CaseResult] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.failed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if r.failed]

    def by_failure_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.failures():
            key = result.failure_kind.value if result.failure_kind else "unknown"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: -item[1]))

    def result_for(self, case_id: str) -> CaseResult | None:
        for result in self.results:
            if result.case_id == case_id:
                return result
        return None

    def summary_table(self) -> str:
        """markdown 汇总, CLI 与页面共用。"""
        lines = [
            f"用例 {self.total} 条 / 通过 {self.passed} / 失败 {self.failed} / "
            f"通过率 {self.pass_rate:.1%} / 耗时 {self.duration_ms:.0f}ms",
        ]
        if self.failures():
            lines += [
                "",
                "| 用例 | 接口 | 失败类型 | 期望 | 实际 |",
                "| --- | --- | --- | --- | --- |",
            ]
            for result in self.failures():
                kind = result.failure_kind.value if result.failure_kind else "-"
                actual = result.http_status if result.http_status is not None else (result.error or "-")
                lines.append(
                    f"| {result.case_id} | `{result.endpoint}` | {kind} | {result.expected} | {actual} |"
                )
        return "\n".join(lines)


TestCase.__test__ = False  # type: ignore[attr-defined]
TestCaseSuite.__test__ = False  # type: ignore[attr-defined]
