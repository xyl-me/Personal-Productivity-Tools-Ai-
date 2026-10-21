"""评测层测试: 规则、场景匹配、综合评分、一致性度量、评审解析。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.evaluator.agreement import (
    LabelPair,
    build_report,
    cohen_kappa,
    kappa_sweep,
    mae,
    spearman,
)
from app.evaluator.judge import JudgeVerdict, build_judge_prompt, judge_endpoint, parse_verdict
from app.evaluator.rules import (
    RuleOutcome,
    evaluate_rules,
    field_specs,
    observations,
    path_values,
    rule_outcomes,
)
from app.evaluator.scenarios import evaluate_scenarios, load_labeled_scenarios
from app.evaluator.scoring import evaluate_endpoint, evaluate_suite, group_by_endpoint
from app.executor.models import Expectation, TestCase, TestCaseSuite
from app.executor.runner import load_case_suite
from app.generator.models import LLMResponse
from app.parser import parse_spec_dict
from target_service.main import app

SPEC = parse_spec_dict(app.openapi())
LABELS = Path(__file__).parents[1] / "data" / "labels" / "scenarios.json"


def endpoint(uid: str):
    found = SPEC.endpoint_index().get(uid)
    assert found is not None
    return found


def case(
    case_id: str,
    *,
    endpoint_uid: str = "POST /api/v1/orders",
    path: str = "/api/v1/orders",
    query: dict | None = None,
    body: object = None,
    headers: dict | None = None,
    status: int = 201,
    name: str = "某个用例名称",
    description: str | None = "覆盖某个场景",
) -> TestCase:
    return TestCase(
        id=case_id,
        name=name,
        endpoint=endpoint_uid,
        method=endpoint_uid.split()[0],
        path=path,
        query=query or {},
        headers=headers or {},
        body=body,
        expect=Expectation(status=status),
        description=description,
    )


def test_field_specs_include_required_flag_and_nested_body() -> None:
    specs = field_specs(endpoint("POST /api/v1/orders"))
    by_path = {spec.path: spec for spec in specs}
    assert "item_id" in by_path
    assert by_path["item_id"].required is True
    assert by_path["remark"].required is False
    assert by_path["remark"].schema.get("maxLength") == 200


def test_path_values_are_recovered_from_concrete_path() -> None:
    values = path_values("/api/v1/orders/{order_id}", "/api/v1/orders/abc")
    assert values == {"order_id": "abc"}


def test_path_values_ignores_mismatched_shape() -> None:
    assert path_values("/api/v1/orders/{order_id}", "/api/v1/orders/1/status") == {}


def test_observations_read_from_each_location() -> None:
    target = endpoint("PUT /api/v1/orders/{order_id}/status")
    samples = [case("C1", endpoint_uid=target.uid, path="/api/v1/orders/1/status", body={"status": "PAID"})]

    body_obs = observations(samples, target, _spec(target, "body", "status"))
    assert body_obs[0].present is True
    assert body_obs[0].value == "PAID"

    path_obs = observations(samples, target, _spec(target, "path", "order_id"))
    assert path_obs[0].present is True
    assert path_obs[0].value == "1"


def test_observations_missing_field() -> None:
    target = endpoint("POST /api/v1/orders")
    samples = [case("C1", body={"quantity": 1})]
    obs = observations(samples, target, _spec(target, "body", "item_id"))
    assert obs[0].present is False


def _spec(target, location: str, path: str):
    from app.evaluator.rules import FieldSpec

    return FieldSpec(location, path, {})


def _outcome(outcomes: list[RuleOutcome], rule_id: str) -> RuleOutcome:
    for outcome in outcomes:
        if outcome.rule_id == rule_id:
            return outcome
    raise AssertionError(f"没有规则 {rule_id}")


def test_positive_rule_requires_a_success_expectation() -> None:
    target = endpoint("GET /api/v1/orders")
    only_failure = [case("C1", endpoint_uid=target.uid, path="/api/v1/orders", status=400)]
    outcomes = rule_outcomes(target, only_failure)
    assert _outcome(outcomes, "positive_baseline").covered is False

    with_success = [*only_failure, case("C2", endpoint_uid=target.uid, path="/api/v1/orders", status=200)]
    assert _outcome(rule_outcomes(target, with_success), "positive_baseline").covered is True


def test_required_missing_rule_detects_omitted_field() -> None:
    target = endpoint("POST /api/v1/orders")
    cases = [case("C1", body={"quantity": 1, "amount": 1.0})]
    assert _outcome(rule_outcomes(target, cases), "required_missing").covered is True

    complete = [case("C2", body={"item_id": "S", "quantity": 1, "amount": 1.0})]
    assert _outcome(rule_outcomes(target, complete), "required_missing").covered is False


def test_length_boundary_requires_every_declared_direction() -> None:
    target = endpoint("POST /api/v1/orders")
    base = {"item_id": "SKU-1", "quantity": 1, "amount": 1.0}
    only_max = [case("C1", body={**base, "remark": "x" * 201})]
    outcome = _outcome(rule_outcomes(target, only_max), "length_boundary")
    assert outcome.covered is False
    assert "item_id" in outcome.note


def test_numeric_boundary_only_min_is_not_enough_for_min_and_max() -> None:
    target = endpoint("GET /api/v1/orders")
    cases = [
        case("C1", endpoint_uid=target.uid, path="/api/v1/orders", query={"page": 0}, status=400),
        case("C2", endpoint_uid=target.uid, path="/api/v1/orders", query={"page_size": -1}, status=400),
    ]
    outcome = _outcome(rule_outcomes(target, cases), "numeric_boundary")
    assert outcome.applicable is False


def test_param_presence_flags_never_used_parameter() -> None:
    spec = parse_spec_dict(
        {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/items": {
                    "get": {
                        "parameters": [
                            {"name": "page", "in": "query", "schema": {"type": "integer"}},
                            {"name": "keyword", "in": "query", "schema": {"type": "string"}},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    target = spec.endpoints[0]
    cases = [case("C1", endpoint_uid="GET /items", path="/items", query={"page": 1}, status=200)]
    outcome = _outcome(rule_outcomes(target, cases), "param_presence")
    assert outcome.covered is False
    assert "query.keyword" in outcome.note


def test_auth_bonus_rules_do_not_lower_required_coverage() -> None:
    """加分维度缺失只影响加分部分, 不该把必测覆盖率拉下来。"""
    target = endpoint("DELETE /api/v1/orders/{order_id}")
    cases = [
        case("C1", endpoint_uid=target.uid, path="/api/v1/orders/1", status=401),
        case(
            "C2",
            endpoint_uid=target.uid,
            path="/api/v1/orders/1",
            status=200,
            headers={"X-Admin-Token": "t"},
        ),
        case("C3", endpoint_uid=target.uid, path="/api/v1/orders/99999", status=404),
    ]
    evaluation = evaluate_rules(target, cases)
    assert evaluation.required_coverage == 100.0
    assert evaluation.bonus_coverage < 100.0
    assert evaluation.rule_score >= 80

    without_auth = evaluate_rules(target, cases[1:])
    assert without_auth.required_coverage < 100.0
    assert without_auth.rule_score < evaluation.rule_score


def test_density_factor_punishes_tiny_sets() -> None:
    target = endpoint("PUT /api/v1/orders/{order_id}/status")
    one = evaluate_rules(
        target, [case("C1", endpoint_uid=target.uid, path="/api/v1/orders/1/status", body={"status": "X"}, status=400)]
    )
    assert one.density_factor < 0.5
    assert one.rule_score < 50


def test_duplicate_cases_reduce_score() -> None:
    target = endpoint("GET /api/v1/orders")
    base = dict(endpoint_uid=target.uid, path="/api/v1/orders", query={"page": 1}, status=200)
    unique = evaluate_rules(target, [case("C1", **base), case("C2", **{**base, "query": {"page": 2}})])
    duped = evaluate_rules(target, [case("C1", **base), case("C2", **base)])
    assert duped.duplicate_ratio > 0
    assert duped.rule_score < unique.rule_score


def test_empty_case_list_is_not_an_error() -> None:
    evaluation = evaluate_rules(endpoint("GET /api/v1/orders"), [])
    assert evaluation.rule_score == 0.0


def test_business_rule_probe_needs_zero_or_negative_value() -> None:
    """文档没写 minimum 不等于不用测: 每个数值业务字段的 0 与负值都要被探测到。"""
    target = endpoint("POST /api/v1/orders")
    only_positive = [
        case("C1", body={"item_id": "S", "quantity": 2, "amount": 10.0}, status=201),
    ]
    outcome = _outcome(rule_outcomes(target, only_positive), "business_rule_probe")
    assert outcome.applicable is True
    assert outcome.covered is False
    assert "amount" in outcome.note

    amount_only = [
        *only_positive,
        case("C2", body={"item_id": "S", "quantity": 1, "amount": -1}, status=400),
    ]
    partial = _outcome(rule_outcomes(target, amount_only), "business_rule_probe")
    assert partial.covered is False
    assert "quantity" in partial.note

    both = [
        *amount_only,
        case("C3", body={"item_id": "S", "quantity": 0, "amount": 1.0}, status=400),
    ]
    assert _outcome(rule_outcomes(target, both), "business_rule_probe").covered is True


def test_business_rule_probe_skips_path_params() -> None:
    """路径参数是标识符, 要求 order_id <= 0 属于噪音, 不算必测。"""
    target = endpoint("DELETE /api/v1/orders/{order_id}")
    cases = [
        case("C1", endpoint_uid=target.uid, path="/api/v1/orders/1", status=401),
        case("C2", endpoint_uid=target.uid, path="/api/v1/orders/1", status=200,
             headers={"X-Admin-Token": "t"}),
    ]
    assert _outcome(rule_outcomes(target, cases), "business_rule_probe").applicable is False


def test_long_string_probe_is_bonus() -> None:
    target = endpoint("GET /api/v1/orders/search")
    short = [case("C1", endpoint_uid=target.uid, path=target.path, query={"q": "SKU-1"}, status=200)]
    outcome = _outcome(rule_outcomes(target, short), "long_string_probe")
    assert outcome.tier == "bonus"
    assert outcome.covered is False

    long_one = [
        *short,
        case("C2", endpoint_uid=target.uid, path=target.path, query={"q": "x" * 200}, status=200),
    ]
    assert _outcome(rule_outcomes(target, long_one), "long_string_probe").covered is True


def test_bonus_cannot_compensate_missing_required() -> None:
    """"深度优先"必须打赢"铺开浅用例" —— 这是 M3 校准的回归锚点。"""
    ai_path = Path(__file__).parents[1] / "cases" / "ai_generated.json"
    if not ai_path.exists():
        pytest.skip("没有真实生成用例集 (先跑 scripts/run_m2.py --real)")

    target = endpoint("POST /api/v1/orders")
    handwritten = [c for c in _handwritten_suite().cases if c.endpoint == target.uid]
    generated = [
        c for c in load_case_suite(ai_path).cases if c.endpoint == target.uid
    ]

    deep = evaluate_rules(target, handwritten)
    shallow = evaluate_rules(target, generated)
    assert deep.rule_score > shallow.rule_score, (
        f"手写 {deep.rule_score} 应当高于铺开的 AI 用例 {shallow.rule_score}"
    )


def test_labeled_scenarios_load_and_cover_all_endpoints() -> None:
    labels = load_labeled_scenarios(LABELS)
    assert set(labels) == {endpoint.uid for endpoint in SPEC.endpoints}
    assert sum(len(items) for items in labels.values()) == 30


def test_scenario_matchers() -> None:
    labels = load_labeled_scenarios(LABELS)
    target = endpoint("POST /api/v1/orders")
    cases = [
        case("C1", status=201, body={"item_id": "S", "quantity": 1, "amount": 10.0}),
        case("C2", status=400, body={"item_id": "S", "quantity": 0, "amount": -1}),
        case("C3", status=201, body={"quantity": 1, "amount": 10.0, "remark": "x" * 30}),
        case("C4", status=400, body={"item_id": "S", "quantity": 1, "amount": "abc"}),
    ]
    coverage = evaluate_scenarios(target, cases, labels[target.uid])
    covered = {item.scenario.id for item in coverage.results if item.covered}
    assert covered == {"S-POST-1", "S-POST-2", "S-POST-3", "S-POST-4", "S-POST-6", "S-POST-7"}
    assert coverage.recall == pytest.approx(6 / 7)


def test_scenario_no_auth_and_status_matchers() -> None:
    labels = load_labeled_scenarios(LABELS)
    target = endpoint("GET /api/v1/admin/orders/export")
    cases = [case("C1", endpoint_uid=target.uid, path=target.path, status=401)]
    coverage = evaluate_scenarios(target, cases, labels[target.uid])
    covered = {item.scenario.id for item in coverage.results if item.covered}
    assert covered == {"S-EXP-1", "S-EXP-3"}
    assert coverage.recall == pytest.approx(2 / 3)


def _handwritten_suite() -> TestCaseSuite:
    data = json.loads((Path(__file__).parents[1] / "cases" / "order_service_cases.json").read_text("utf-8"))
    return TestCaseSuite.model_validate(data)


def test_evaluate_suite_scores_every_endpoint_with_cases() -> None:
    suite = _handwritten_suite()
    score = evaluate_suite(SPEC, suite, scenarios=load_labeled_scenarios(LABELS))
    assert len(score.endpoints) == 7
    assert score.total_cases == 18
    assert 0 < score.mean_score < 100


def test_score_without_llm_equals_rule_score() -> None:
    suite = _handwritten_suite()
    score = evaluate_suite(SPEC, suite)
    for item in score.endpoints:
        assert item.final_score == item.rule.rule_score
        assert item.llm is None


def test_llm_verdict_pulls_final_score() -> None:
    suite = _handwritten_suite()
    grouped = group_by_endpoint(suite.cases)
    target = endpoint("PUT /api/v1/orders/{order_id}/status")
    verdict = JudgeVerdict.model_validate({"score": 100, "strengths": [], "issues": []})
    with_llm = evaluate_endpoint(target, grouped[target.uid], llm=verdict, llm_weight=0.3)
    without = evaluate_endpoint(target, grouped[target.uid])
    assert with_llm.final_score > without.final_score


def test_verdict_follows_threshold() -> None:
    suite = _handwritten_suite()
    grouped = group_by_endpoint(suite.cases)
    target = endpoint("PUT /api/v1/orders/{order_id}/status")
    strict = evaluate_endpoint(target, grouped[target.uid], threshold=10)
    loose = evaluate_endpoint(target, grouped[target.uid], threshold=99)
    assert strict.verdict == "AI已验证"
    assert loose.verdict == "需人工复核"


def test_review_reasons_are_human_readable() -> None:
    suite = _handwritten_suite()
    score = evaluate_suite(SPEC, suite, scenarios=load_labeled_scenarios(LABELS))
    reviewed = [item for item in score.endpoints if not item.passes]
    assert reviewed
    assert all(item.review_reasons for item in reviewed)
    assert any("未覆盖" in reason for item in reviewed for reason in item.review_reasons)


def test_spearman_perfect_and_inverse() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0


def test_spearman_handles_ties() -> None:
    assert spearman([1, 2, 2, 3], [1, 2, 2, 3]) == 1.0


def test_spearman_needs_variance() -> None:
    assert spearman([5, 5, 5], [1, 2, 3]) == 0.0


def test_mae() -> None:
    assert mae([10, 20], [12, 18]) == 2.0


def test_kappa_perfect_and_chance() -> None:
    assert cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    assert cohen_kappa([1, 0, 1, 0], [0, 1, 0, 1]) < 0


def test_kappa_sweep_covers_thresholds() -> None:
    pairs = [
        LabelPair("a", 90, 88, True, True),
        LabelPair("b", 75, 70, False, False),
        LabelPair("c", 60, 85, False, True),
    ]
    sweep = kappa_sweep(pairs, thresholds=[60, 80])
    assert [row[0] for row in sweep] == [60.0, 80.0]
    assert all(-1 <= row[1] <= 1 for row in sweep)


def test_agreement_report_renders_verdict() -> None:
    pairs = [
        LabelPair("a", 90, 88, True, True),
        LabelPair("b", 40, 45, False, False),
    ]
    text = build_report(pairs).render()
    assert "Spearman" in text and "κ" in text


def test_parse_verdict_strips_fence_and_clamps() -> None:
    verdict = parse_verdict('```json\n{"score": 130, "issues": []}\n```')
    assert verdict.score == 100


def test_parse_verdict_rejects_garbage() -> None:
    from app.common.jsonutil import JsonParseError

    with pytest.raises(JsonParseError):
        parse_verdict("完全不是 JSON")
    with pytest.raises(JsonParseError):
        parse_verdict('{"issues": []}')


def test_judge_prompt_contains_cases_and_instructions() -> None:
    from app.evaluator.judge import JUDGE_SYSTEM_PROMPT

    suite = _handwritten_suite()
    grouped = group_by_endpoint(suite.cases)
    target = endpoint("POST /api/v1/orders")
    prompt = build_judge_prompt(target, grouped[target.uid])
    assert "待评审用例" in prompt
    assert "C01" in prompt
    assert "已由程序精确判定" in JUDGE_SYSTEM_PROMPT


class _BrokenClient:
    mode = "real"
    model = "broken"

    async def complete(self, messages, *, temperature, max_tokens):
        raise RuntimeError("401 invalid api key")


class _GoodClient:
    mode = "real"
    model = "good"

    async def complete(self, messages, *, temperature, max_tokens):
        return LLMResponse(
            text=json.dumps({"score": 66, "strengths": ["覆盖正向"], "issues": []}, ensure_ascii=False),
            model="good",
            prompt_tokens=10,
            completion_tokens=5,
        )


def test_judge_endpoint_records_error_without_raising() -> None:
    import asyncio

    suite = _handwritten_suite()
    grouped = group_by_endpoint(suite.cases)
    target = endpoint("POST /api/v1/orders")
    record = asyncio.run(judge_endpoint(target, grouped[target.uid], _BrokenClient()))
    assert record.verdict is None
    assert record.error is not None and "401" in record.error


def test_judge_endpoint_parses_verdict() -> None:
    import asyncio

    suite = _handwritten_suite()
    grouped = group_by_endpoint(suite.cases)
    target = endpoint("POST /api/v1/orders")
    record = asyncio.run(judge_endpoint(target, grouped[target.uid], _GoodClient()))
    assert record.verdict is not None and record.verdict.score == 66
    assert record.completion_tokens == 5
