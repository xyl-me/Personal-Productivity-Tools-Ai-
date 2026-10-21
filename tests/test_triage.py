"""缺陷归集与指标计算的单元测试 (不依赖网络, 全部用合成数据)。"""

from __future__ import annotations

from app.executor.ground_truth import GROUND_TRUTH
from app.executor.models import (
    CaseResult,
    CaseStatus,
    ExecutionReport,
    FailureKind,
    TestCase,
)
from app.executor.triage import DefectSignature, attribute, evaluate


def make_case(
    case_id: str,
    *,
    endpoint: str = "POST /x",
    tags: list[str] | None = None,
    body: object = None,
    query: dict[str, object] | None = None,
) -> TestCase:
    return TestCase(
        id=case_id,
        name=case_id,
        endpoint=endpoint,
        method=endpoint.split()[0],
        path="/x",
        tags=tags or [],
        body=body,
        query=query or {},
    )


def make_result(
    case: TestCase,
    kind: FailureKind | None,
    *,
    http_status: int | None = 500,
    status: CaseStatus | None = None,
) -> CaseResult:
    if status is None:
        status = CaseStatus.FAILED if kind is not None else CaseStatus.PASSED
    return CaseResult(
        case_id=case.id,
        name=case.name,
        endpoint=case.endpoint,
        method=case.method,
        path=case.path,
        status=status,
        failure_kind=kind,
        http_status=http_status,
    )


def report_of(results: list[CaseResult]) -> ExecutionReport:
    return ExecutionReport(results=results)


SIG_A = DefectSignature(
    id="A",
    title="服务端崩溃",
    category="稳定性",
    endpoint="POST /x",
    failure_kinds=frozenset({FailureKind.SERVER_ERROR}),
)
SIG_B = DefectSignature(
    id="B",
    title="负数被接受",
    category="业务规则",
    endpoint="POST /x",
    failure_kinds=frozenset({FailureKind.UNEXPECTED_SUCCESS}),
    extra=lambda case, result: isinstance(case.body, dict) and case.body.get("n", 0) < 0,
)


def test_recall_and_missed() -> None:
    hit = make_case("C1")
    evaluation = evaluate(
        [hit],
        report_of([make_result(hit, FailureKind.SERVER_ERROR)]),
        [SIG_A, SIG_B],
    )
    assert evaluation.hits["A"] == ["C1"]
    assert [d.id for d in evaluation.missed] == ["B"]
    assert evaluation.recall == 0.5


def test_duplicate_hits_measured() -> None:
    c1, c2 = make_case("C1"), make_case("C2")
    evaluation = evaluate(
        [c1, c2],
        report_of([make_result(c1, FailureKind.SERVER_ERROR), make_result(c2, FailureKind.SERVER_ERROR)]),
        [SIG_A],
    )
    assert evaluation.attributed_cases == 2
    assert evaluation.duplicate_hits == 1
    assert evaluation.defects_hit_twice_or_more() == [("A", 2)]


def test_baseline_failure_without_match_is_false_positive() -> None:
    control = make_case("K1", tags=["control"])
    evaluation = evaluate(
        [control],
        report_of([make_result(control, FailureKind.STATUS_MISMATCH, http_status=403)]),
        [SIG_A],
    )
    assert [r.case_id for r in evaluation.false_positives] == ["K1"]
    assert evaluation.baseline_total == 1
    assert evaluation.false_positive_rate == 1.0
    assert evaluation.unattributed == []


def test_baseline_failure_that_matches_a_defect_is_not_a_false_positive() -> None:
    """对照组用例撞上已知缺陷, 是缺陷漏到了主流程, 不该算用例误报。"""
    control = make_case("K2", tags=["positive"])
    evaluation = evaluate(
        [control],
        report_of([make_result(control, FailureKind.SERVER_ERROR)]),
        [SIG_A],
    )
    assert evaluation.false_positives == []
    assert evaluation.hits["A"] == ["K2"]


def test_non_baseline_unmatched_failure_is_unattributed() -> None:
    probe = make_case("P1")
    evaluation = evaluate(
        [probe],
        report_of([make_result(probe, FailureKind.STATUS_MISMATCH)]),
        [SIG_A],
    )
    assert [r.case_id for r in evaluation.unattributed] == ["P1"]
    assert evaluation.false_positives == []


def test_execution_errors_are_excluded_from_all_buckets() -> None:
    probe = make_case("P2", tags=["control"])
    evaluation = evaluate(
        [probe],
        report_of([make_result(probe, FailureKind.TRANSPORT_ERROR, http_status=None, status=CaseStatus.ERROR)]),
        [SIG_A],
    )
    assert [r.case_id for r in evaluation.execution_errors] == ["P2"]
    assert evaluation.false_positives == []
    assert evaluation.unattributed == []


def test_same_endpoint_same_kind_disambiguated_by_request_feature() -> None:
    positive_number = make_case("C1", body={"n": 1})
    negative_number = make_case("C2", body={"n": -1})
    results = [
        make_result(positive_number, FailureKind.UNEXPECTED_SUCCESS, http_status=201),
        make_result(negative_number, FailureKind.UNEXPECTED_SUCCESS, http_status=201),
    ]
    evaluation = evaluate([positive_number, negative_number], report_of(results), [SIG_B])
    assert evaluation.hits["B"] == ["C2"]
    assert [r.case_id for r in evaluation.unattributed] == ["C1"]


def test_passed_result_is_never_attributed() -> None:
    case = make_case("C1")
    assert attribute(case, make_result(case, None, http_status=200), [SIG_A]) is None


def test_render_shows_missed_and_metrics() -> None:
    hit = make_case("C1")
    text = evaluate([hit], report_of([make_result(hit, FailureKind.SERVER_ERROR)]), [SIG_A, SIG_B]).render()
    assert "漏测" in text
    assert "缺陷召回率: 1/2 = 50.0%" in text


def test_ground_truth_covers_every_seeded_defect_id() -> None:
    """SEED_DEFECTS.md 里写了 D1~D8, 签名表必须一条不多一条不少。"""
    assert [d.id for d in GROUND_TRUTH] == [f"D{i}" for i in range(1, 9)]
    assert all(d.endpoint.startswith(("GET ", "POST ", "PUT ", "DELETE ")) for d in GROUND_TRUTH)
