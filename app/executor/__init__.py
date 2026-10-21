"""执行层: 把用例真的打出去, 然后判定结果、归集缺陷。"""

from .models import (
    CaseResult,
    CaseStatus,
    ExecutionReport,
    Expectation,
    FailureKind,
    TestCase,
    TestCaseSuite,
)

__all__ = [
    "CaseResult",
    "CaseStatus",
    "ExecutionReport",
    "Expectation",
    "FailureKind",
    "TestCase",
    "TestCaseSuite",
]
