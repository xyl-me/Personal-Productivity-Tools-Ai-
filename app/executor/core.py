"""单条用例的执行与判定。"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .models import CaseResult, CaseStatus, Expectation, FailureKind, TestCase

SNIPPET_LIMIT = 400


def _is_subset(expected: Any, actual: Any) -> bool:
    """递归子集匹配: 期望里出现的字段必须存在且相等, 允许多余字段。"""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(key in actual and _is_subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(_is_subset(e, a) for e, a in zip(expected, actual))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    return expected == actual


def classify(expect: Expectation, response: httpx.Response) -> FailureKind | None:
    """返回 ``None`` 表示通过。"""
    status = response.status_code
    if status >= 500:
        return FailureKind.SERVER_ERROR

    statuses = expect.statuses
    if statuses and status not in statuses:
        if expect.is_negative and status < 400:
            return FailureKind.UNEXPECTED_SUCCESS
        return FailureKind.STATUS_MISMATCH

    if expect.body_contains:
        text = response.text
        if any(fragment not in text for fragment in expect.body_contains):
            return FailureKind.CONTENT_MISMATCH

    if expect.json_contains is not None:
        try:
            payload = response.json()
        except ValueError:
            return FailureKind.CONTENT_MISMATCH
        if not _is_subset(expect.json_contains, payload):
            return FailureKind.CONTENT_MISMATCH

    if expect.json_length is not None:
        try:
            payload = response.json()
        except ValueError:
            return FailureKind.CONTENT_MISMATCH
        if not isinstance(payload, list) or len(payload) != expect.json_length:
            return FailureKind.CONTENT_MISMATCH

    return None


def _explain(kind: FailureKind, expect: Expectation, response: httpx.Response) -> str:
    status = response.status_code
    expected = "/".join(str(code) for code in expect.statuses) or "(未断言状态码)"
    if kind is FailureKind.SERVER_ERROR:
        return f"服务端返回 {status} (期望 {expected})"
    if kind is FailureKind.UNEXPECTED_SUCCESS:
        return f"期望被拒绝 ({expected}), 实际成功 {status}"
    if kind is FailureKind.STATUS_MISMATCH:
        return f"状态码不匹配: 期望 {expected}, 实际 {status}"
    if kind is FailureKind.CONTENT_MISMATCH:
        return f"响应内容不符合断言: {expect.describe()}"
    return kind.value


def _result(
    case: TestCase,
    *,
    status: CaseStatus,
    failure_kind: FailureKind | None,
    duration_ms: float,
    http_status: int | None = None,
    error: str | None = None,
    snippet: str = "",
) -> CaseResult:
    return CaseResult(
        case_id=case.id,
        name=case.name,
        endpoint=case.endpoint,
        method=case.method,
        path=case.path,
        status=status,
        failure_kind=failure_kind,
        http_status=http_status,
        duration_ms=round(duration_ms, 1),
        error=error,
        response_snippet=snippet,
        expected=case.expect.describe(),
    )


def execute_case(case: TestCase, client: httpx.Client) -> CaseResult:
    """执行一条用例。``client`` 需要已经绑定 base_url。"""
    started = time.perf_counter()
    try:
        response = client.request(
            case.method,
            case.path,
            params=case.query or None,
            headers=case.headers or None,
            json=case.body if case.body is not None else None,
        )
    except httpx.TimeoutException as exc:
        return _result(
            case,
            status=CaseStatus.ERROR,
            failure_kind=FailureKind.TIMEOUT,
            duration_ms=(time.perf_counter() - started) * 1000,
            error=f"请求超时: {exc}",
        )
    except httpx.HTTPError as exc:
        return _result(
            case,
            status=CaseStatus.ERROR,
            failure_kind=FailureKind.TRANSPORT_ERROR,
            duration_ms=(time.perf_counter() - started) * 1000,
            error=f"请求失败: {exc}",
        )

    duration_ms = (time.perf_counter() - started) * 1000
    kind = classify(case.expect, response)
    return _result(
        case,
        status=CaseStatus.PASSED if kind is None else CaseStatus.FAILED,
        failure_kind=kind,
        duration_ms=duration_ms,
        http_status=response.status_code,
        error=None if kind is None else _explain(kind, case.expect, response),
        snippet=response.text[:SNIPPET_LIMIT],
    )
