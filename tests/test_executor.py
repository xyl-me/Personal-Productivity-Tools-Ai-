"""执行层单元测试: 判定逻辑 + 单用例执行 + 用例集加载。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.executor.core import classify, execute_case, _is_subset
from app.executor.live import free_port
from app.executor.models import CaseStatus, Expectation, FailureKind, TestCase, TestCaseSuite
from app.executor.runner import load_case_suite, run_cases
from app.parser import parse_spec_dict
from target_service.main import app, reset_demo_data

CASES_FILE = Path(__file__).parents[1] / "cases" / "order_service_cases.json"


def _response(status: int, body: str = "", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=body.encode("utf-8"),
        headers=headers or {"content-type": "application/json"},
        request=httpx.Request("GET", "http://testserver/x"),
    )


def test_pass_when_status_matches() -> None:
    assert classify(Expectation(status=200), _response(200)) is None


def test_server_error_wins_over_status_match() -> None:
    """即使用例期望的就是 500, 也要判成 SERVER_ERROR —— 崩溃必须被看见。"""
    assert classify(Expectation(status=500), _response(500)) is FailureKind.SERVER_ERROR


def test_expected_rejection_but_succeeded() -> None:
    kind = classify(Expectation(status=400), _response(201))
    assert kind is FailureKind.UNEXPECTED_SUCCESS


def test_expected_400_but_got_404_is_plain_mismatch() -> None:
    """方向不同: 更严格地拒绝了, 不是"放过请求", 所以不算 UNEXPECTED_SUCCESS。"""
    assert classify(Expectation(status=400), _response(404)) is FailureKind.STATUS_MISMATCH


def test_expected_success_but_rejected() -> None:
    assert classify(Expectation(status=200), _response(403)) is FailureKind.STATUS_MISMATCH


def test_content_mismatch_on_missing_field() -> None:
    kind = classify(
        Expectation(status=200, json_contains={"id": 1}), _response(200, '{"id": 2}')
    )
    assert kind is FailureKind.CONTENT_MISMATCH


def test_content_mismatch_on_wrong_length() -> None:
    kind = classify(Expectation(status=200, json_length=1), _response(200, "[{},{}]"))
    assert kind is FailureKind.CONTENT_MISMATCH


def test_content_mismatch_on_invalid_json() -> None:
    kind = classify(Expectation(status=200, json_length=1), _response(200, "not json"))
    assert kind is FailureKind.CONTENT_MISMATCH


def test_body_contains_fragment() -> None:
    assert classify(Expectation(body_contains=["订单不存在"]), _response(404, '{"detail":"订单不存在"}')) is None
    assert (
        classify(Expectation(body_contains=["订单不存在"]), _response(404, '{"detail":"x"}'))
        is FailureKind.CONTENT_MISMATCH
    )


@pytest.mark.parametrize(
    ("expected", "actual", "ok"),
    [
        ({"a": 1}, {"a": 1, "b": 2}, True),
        ({"a": 1}, {"a": 2}, False),
        ({"a": {"b": 1}}, {"a": {"b": 1, "c": 3}}, True),
        ({"a": {"b": 1}}, {"a": {"b": 2}}, False),
        ({}, {"x": 1}, True),
        ([1, 2], [1, 2], True),
        ([1, 2], [1, 2, 3], False),
        (True, 1, False),
    ],
)
def test_is_subset(expected: object, actual: object, ok: bool) -> None:
    assert _is_subset(expected, actual) is ok


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_execute_case_passes() -> None:
    reset_demo_data()
    case = TestCase(
        id="T1",
        name="列表",
        endpoint="GET /api/v1/orders",
        method="GET",
        path="/api/v1/orders",
        expect=Expectation(status=200, json_length=3),
    )
    result = execute_case(case, _client())
    assert result.status is CaseStatus.PASSED
    assert result.http_status == 200
    assert result.failure_kind is None


def test_execute_case_captures_failure_reason() -> None:
    reset_demo_data()
    case = TestCase(
        id="T2",
        name="非数字 ID",
        endpoint="GET /api/v1/orders/{order_id}",
        method="GET",
        path="/api/v1/orders/abc",
        expect=Expectation(status=404),
    )
    result = execute_case(case, _client())
    assert result.status is CaseStatus.FAILED
    assert result.failure_kind is FailureKind.SERVER_ERROR
    assert result.http_status == 500
    assert "500" in (result.error or "")


class _BrokenClient:
    """只实现 request(), 用来确定性地构造传输层异常。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def request(self, *args: object, **kwargs: object) -> object:
        raise self._exc


def test_connect_failure_is_execution_error() -> None:
    case = TestCase(
        id="T3", name="连不上", endpoint="GET /x", method="GET", path="/x", expect=Expectation(status=200)
    )
    result = execute_case(case, _BrokenClient(httpx.ConnectError("connection refused")))  # type: ignore[arg-type]
    assert result.status is CaseStatus.ERROR
    assert result.failure_kind is FailureKind.TRANSPORT_ERROR
    assert result.http_status is None


def test_read_timeout_is_execution_error() -> None:
    case = TestCase(
        id="T3b", name="超时", endpoint="GET /x", method="GET", path="/x", expect=Expectation(status=200)
    )
    result = execute_case(case, _BrokenClient(httpx.ReadTimeout("slow")))  # type: ignore[arg-type]
    assert result.status is CaseStatus.ERROR
    assert result.failure_kind is FailureKind.TIMEOUT


def test_unreachable_target_counts_as_error_not_failure() -> None:
    """真实网络下连不通可能表现为 refused, 也可能被静默丢包变成超时。"""
    case = TestCase(
        id="T3c", name="不可达", endpoint="GET /x", method="GET", path="/x", expect=Expectation(status=200)
    )
    with httpx.Client(base_url=f"http://127.0.0.1:{free_port()}", timeout=1) as client:
        result = execute_case(case, client)
    assert result.status is CaseStatus.ERROR
    assert result.failure_kind in {FailureKind.TRANSPORT_ERROR, FailureKind.TIMEOUT}


def test_execute_case_sends_query_and_headers() -> None:
    reset_demo_data()
    case = TestCase(
        id="T4",
        name="带 token 删除",
        endpoint="DELETE /api/v1/orders/{order_id}",
        method="DELETE",
        path="/api/v1/orders/1",
        headers={"X-Admin-Token": "secret-admin-token"},
        expect=Expectation(status=200),
    )
    assert execute_case(case, _client()).status is CaseStatus.PASSED


def test_run_cases_calls_reset_before_every_case() -> None:
    calls: list[int] = []

    def reset() -> None:
        calls.append(1)
        reset_demo_data()

    cases = [
        TestCase(id=f"R{i}", name="列表", endpoint="GET /api/v1/orders",
                 method="GET", path="/api/v1/orders", expect=Expectation(status=200))
        for i in range(3)
    ]
    report = run_cases(cases, client=_client(), reset=reset)
    assert len(calls) == 3
    assert report.total == 3
    assert report.passed == 3


def test_run_cases_summary_table_contains_failures() -> None:
    cases = [
        TestCase(id="S1", name="非法 ID", endpoint="GET /api/v1/orders/{order_id}",
                 method="GET", path="/api/v1/orders/abc", expect=Expectation(status=404)),
    ]
    report = run_cases(cases, client=_client(), reset=reset_demo_data)
    table = report.summary_table()
    assert "S1" in table and "server_error" in table


def test_case_file_loads_and_ids_are_unique() -> None:
    suite = load_case_suite(CASES_FILE)
    assert isinstance(suite, TestCaseSuite)
    assert len(suite) == 18
    ids = [case.id for case in suite.cases]
    assert len(ids) == len(set(ids))


def test_every_case_targets_a_documented_endpoint() -> None:
    """用例里的 endpoint 必须能在文档里找到, 否则归因和统计都会失真。"""
    documented = {endpoint.uid for endpoint in parse_spec_dict(app.openapi()).endpoints}
    suite = load_case_suite(CASES_FILE)
    unknown = {case.endpoint for case in suite.cases} - documented
    assert unknown == set()


def test_case_file_has_both_baseline_and_probes() -> None:
    suite = load_case_suite(CASES_FILE)
    assert sum(1 for case in suite.cases if case.is_control) == 5
    assert sum(1 for case in suite.cases if case.is_positive) == 4
    assert sum(1 for case in suite.cases if case.is_baseline) == 9
