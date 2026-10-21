"""M1 端到端验收: 真实 HTTP 服务 + 手写用例 + 缺陷归集。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.executor.ground_truth import GROUND_TRUTH
from app.executor.live import live_server
from app.executor.runner import is_loopback, load_case_suite, run_cases
from app.executor.triage import evaluate
from target_service.main import app, reset_demo_data

CASES_FILE = Path(__file__).parents[1] / "cases" / "order_service_cases.json"
SUITE = load_case_suite(CASES_FILE)


@pytest.fixture(scope="module")
def report():
    """整个模块只起一次服务、跑一次全量用例。"""
    with live_server(app) as base_url:
        return run_cases(SUITE, base_url=base_url, reset=reset_demo_data)


def test_live_target_really_speaks_http() -> None:
    """确认走的是真实 TCP, 而不是 ASGI 内存传输 —— 否则这组测试的说服力是假的。"""
    with live_server(app) as base_url:
        response = httpx.get(
            f"{base_url}/api/v1/orders", timeout=5, trust_env=not is_loopback(base_url)
        )
    assert response.status_code == 200
    assert "uvicorn" in response.headers.get("server", "")


def test_loopback_execution_ignores_dead_proxy(monkeypatch) -> None:
    """开了 VPN 后系统代理会把 127.0.0.1 也送去代理, 导致全部请求 502。

    这里故意把代理指向一个不可用端口并清掉 NO_PROXY:
    只有执行器真的对本机地址关掉了 trust_env, 用例才跑得通。
    """
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")

    suite = load_case_suite(CASES_FILE)
    with live_server(app) as base_url:
        report = run_cases(suite, base_url=base_url, reset=reset_demo_data)

    assert report.total == len(suite)
    assert report.passed >= 1


def test_suite_shape() -> None:
    assert len(SUITE) == 18
    assert sum(1 for case in SUITE.cases if case.is_baseline) == 9


def test_all_eight_defects_are_detected(report) -> None:
    evaluation = evaluate(SUITE, report, GROUND_TRUTH)
    assert evaluation.missed == []
    assert evaluation.detected == 8
    assert evaluation.recall == 1.0


def test_no_false_positives(report) -> None:
    evaluation = evaluate(SUITE, report, GROUND_TRUTH)
    assert evaluation.false_positives == []
    assert evaluation.false_positive_rate == 0.0


def test_no_unattributed_failures(report) -> None:
    """每一条失败的用例都能被解释: 要么命中已知缺陷, 要么是误报。没有"搞不清为什么"的。"""
    evaluation = evaluate(SUITE, report, GROUND_TRUTH)
    assert evaluation.unattributed == []
    assert evaluation.execution_errors == []


def test_same_defect_hit_by_multiple_cases(report) -> None:
    evaluation = evaluate(SUITE, report, GROUND_TRUTH)
    assert evaluation.hits["D5"] == ["C14", "C15"]
    assert evaluation.duplicate_hits == 1


def test_pass_fail_split(report) -> None:
    assert report.total == 18
    assert report.passed == 9
    assert report.failed == 9
    assert report.by_failure_kind() == {
        "unexpected_success": 6,
        "server_error": 2,
        "content_mismatch": 1,
    }


def test_execution_is_repeatable(report) -> None:
    """两条完全一样的执行结果, 才能说"这是稳定复现的缺陷"而不是偶发。"""
    with live_server(app) as base_url:
        again = run_cases(SUITE, base_url=base_url, reset=reset_demo_data)

    def fingerprint(rep) -> list[tuple[str, str, str | None]]:
        return [(r.case_id, r.status.value, r.failure_kind.value if r.failure_kind else None)
                for r in rep.results]

    assert fingerprint(again) == fingerprint(report)
