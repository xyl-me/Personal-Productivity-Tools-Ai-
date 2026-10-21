"""文档 diff 与增量生成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.evaluator.changes import evaluate_change_coverage
from app.executor.models import Expectation, TestCase, TestCaseSuite
from app.generator.incremental import (
    TAG_ADDED,
    TAG_BREAKING,
    TAG_CHANGED,
    TAG_INCREMENTAL,
    build_plan,
    tag_incremental,
)
from app.parser import parse_spec_dict, parse_spec_file
from app.parser.diff import SpecDiff, diff_specs

SPECS = Path(__file__).parents[1] / "fixtures" / "specs"


def _spec(paths: dict, schemas: dict | None = None, *, title: str = "t"):
    return parse_spec_dict(
        {
            "openapi": "3.0.3",
            "info": {"title": title, "version": "1"},
            "paths": paths,
            "components": {"schemas": schemas or {}},
        }
    )


def _endpoint(body_schema: dict | None = None, *, params: list | None = None, status: str = "200"):
    operation: dict = {
        "responses": {status: {"description": "ok"}},
        "parameters": params or [],
    }
    if body_schema is not None:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": body_schema}},
        }
    return {"post": operation}


def _changes(diff: SpecDiff, uid: str) -> list[str]:
    for item in diff.changed:
        if item.uid == uid:
            return [f"{c.location}.{c.field_name}:{c.kind}:{c.severity}" for c in item.changes]
    raise AssertionError(f"{uid} 没有被标记为变更")


def test_identical_specs_have_no_changes() -> None:
    paths = {"/x": _endpoint({"type": "object", "properties": {"a": {"type": "string"}}})}
    diff = diff_specs(_spec(paths), _spec(paths))
    assert diff.added == [] and diff.removed == [] and diff.changed == []
    assert diff.impacted_uids == []
    assert diff.unchanged[0].uid == "POST /x"


def test_added_and_removed_endpoints() -> None:
    old = _spec({"/a": _endpoint()})
    new = _spec({"/b": _endpoint()})
    diff = diff_specs(old, new)
    assert [item.uid for item in diff.added] == ["POST /b"]
    assert [item.uid for item in diff.removed] == ["POST /a"]
    assert diff.impacted_uids == ["POST /b"]
    assert diff.breaking_count == 1


def test_generation_saving_reflects_impacted_ratio() -> None:
    paths = {f"/r{i}": _endpoint() for i in range(10)}
    assert diff_specs(_spec(paths), _spec(paths)).generation_saving == pytest.approx(1.0)

    changed = dict(paths)
    changed["/r0"] = _endpoint({"type": "object", "properties": {"a": {"type": "string"}}})
    diff = diff_specs(_spec(paths), _spec(changed))
    assert diff.impacted_uids == ["POST /r0"]
    assert diff.generation_saving == pytest.approx(0.9)


def test_body_field_added_is_relaxed() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    schema2 = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    diff = diff_specs(_spec({"/x": _endpoint(schema)}), _spec({"/x": _endpoint(schema2)}))
    assert _changes(diff, "POST /x") == ["body.b:added:relaxed"]


def test_field_removed_is_breaking() -> None:
    one = {"type": "object", "properties": {"a": {"type": "string"}}}
    two = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    diff = diff_specs(_spec({"/x": _endpoint(two)}), _spec({"/x": _endpoint(one)}))
    assert _changes(diff, "POST /x") == ["body.b:removed:breaking"]


def test_max_length_tightened_is_breaking_and_loosened_is_relaxed() -> None:
    loose = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 200}}}
    tight = {"type": "object", "properties": {"a": {"type": "string", "maxLength": 120}}}
    tightened = diff_specs(_spec({"/x": _endpoint(loose)}), _spec({"/x": _endpoint(tight)}))
    assert _changes(tightened, "POST /x") == ["body.a:maxLength:breaking"]

    relaxed = diff_specs(_spec({"/x": _endpoint(tight)}), _spec({"/x": _endpoint(loose)}))
    assert _changes(relaxed, "POST /x") == ["body.a:maxLength:relaxed"]


def test_new_minimum_is_breaking() -> None:
    before = {"type": "object", "properties": {"q": {"type": "integer"}}}
    after = {"type": "object", "properties": {"q": {"type": "integer", "minimum": 1}}}
    diff = diff_specs(_spec({"/x": _endpoint(before)}), _spec({"/x": _endpoint(after)}))
    assert _changes(diff, "POST /x") == ["body.q:minimum:breaking"]


def test_enum_narrowed_is_breaking_widened_is_relaxed() -> None:
    wide = {"type": "object", "properties": {"s": {"type": "string", "enum": ["a", "b", "c"]}}}
    narrow = {"type": "object", "properties": {"s": {"type": "string", "enum": ["a", "b"]}}}
    assert _changes(diff_specs(_spec({"/x": _endpoint(wide)}), _spec({"/x": _endpoint(narrow)})), "POST /x") == [
        "body.s:enum:breaking"
    ]
    assert _changes(diff_specs(_spec({"/x": _endpoint(narrow)}), _spec({"/x": _endpoint(wide)})), "POST /x") == [
        "body.s:enum:relaxed"
    ]


def test_required_flag_change() -> None:
    optional = {"type": "object", "properties": {"a": {"type": "string"}}}
    required = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    diff = diff_specs(_spec({"/x": _endpoint(optional)}), _spec({"/x": _endpoint(required)}))
    assert _changes(diff, "POST /x") == ["body.a:required:breaking"]


def test_query_parameter_added_and_type_changed() -> None:
    before = _endpoint(params=[{"name": "page", "in": "query", "schema": {"type": "integer"}}])
    after = _endpoint(
        params=[
            {"name": "page", "in": "query", "schema": {"type": "string"}},
            {"name": "size", "in": "query", "schema": {"type": "integer"}},
        ]
    )
    diff = diff_specs(_spec({"/x": before}), _spec({"/x": after}))
    assert set(_changes(diff, "POST /x")) == {
        "query.page:type:breaking",
        "query.size:added:relaxed",
    }


def test_response_code_removed_is_breaking() -> None:
    old = {"post": {"responses": {"200": {"description": "ok"}, "400": {"description": "bad"}}}}
    new = {"post": {"responses": {"200": {"description": "ok"}}}}
    diff = diff_specs(_spec({"/x": old}), _spec({"/x": new}))
    assert "response.400:response:breaking" in _changes(diff, "POST /x")


def test_real_version_pair() -> None:
    old = parse_spec_file(SPECS / "orders_v1.json")
    new = parse_spec_file(SPECS / "orders_v2.json")
    plan = build_plan(old, new)

    assert plan.diff.added[0].uid == "PATCH /api/v1/orders/{order_id}/remark"
    assert plan.removed == ["GET /api/v1/orders/search"]
    assert set(plan.endpoints) == {
        "PATCH /api/v1/orders/{order_id}/remark",
        "GET /api/v1/orders",
        "POST /api/v1/orders",
    }
    assert "DELETE /api/v1/orders/{order_id}" not in plan.endpoints
    assert plan.saving == pytest.approx(1 - 3 / 8)


def test_tag_incremental_marks_cases() -> None:
    old = parse_spec_file(SPECS / "orders_v1.json")
    new = parse_spec_file(SPECS / "orders_v2.json")
    plan = build_plan(old, new)

    cases = [
        TestCase(
            id="A1",
            name="建单",
            endpoint="POST /api/v1/orders",
            method="POST",
            path="/api/v1/orders",
            expect=Expectation(status=201),
        ),
        TestCase(
            id="A2",
            name="未变更接口",
            endpoint="DELETE /api/v1/orders/{order_id}",
            method="DELETE",
            path="/api/v1/orders/1",
            expect=Expectation(status=200),
        ),
    ]
    suite = tag_incremental(TestCaseSuite(suite="s", cases=cases), plan.diff)

    assert TAG_INCREMENTAL in suite.cases[0].tags
    assert TAG_CHANGED in suite.cases[0].tags
    assert TAG_BREAKING in suite.cases[0].tags
    assert suite.cases[1].tags == []


def test_change_coverage_flags_untouched_fields() -> None:
    old = parse_spec_file(SPECS / "orders_v1.json")
    new = parse_spec_file(SPECS / "orders_v2.json")
    diff = build_plan(old, new).diff

    shallow = TestCaseSuite(
        suite="s",
        cases=[
            TestCase(
                id="A1",
                name="建单",
                endpoint="POST /api/v1/orders",
                method="POST",
                path="/api/v1/orders",
                body={"item_id": "S", "quantity": 1, "amount": 1.0},
                expect=Expectation(status=201),
            )
        ],
    )
    coverage = evaluate_change_coverage(shallow, diff, new)
    assert coverage.covered == 1
    assert coverage.total == 5
    assert coverage.ratio < 1.0
    missed = {(miss.location, miss.field_name) for miss in coverage.misses}
    assert ("body", "remark") in missed
    assert ("body", "channel") in missed
    assert ("query", "page_size") in missed
    assert "未覆盖" not in coverage.render().splitlines()[0]


def test_change_coverage_full_when_all_touched() -> None:
    old = parse_spec_file(SPECS / "orders_v1.json")
    new = parse_spec_file(SPECS / "orders_v2.json")
    diff = build_plan(old, new).diff

    def case(case_id: str, uid: str, **kwargs) -> TestCase:
        return TestCase(
            id=case_id,
            name=case_id,
            endpoint=uid,
            method=uid.split()[0],
            path=kwargs.pop("path", "/api/v1/orders"),
            expect=Expectation(status=200),
            **kwargs,
        )

    suite = TestCaseSuite(
        suite="s",
        cases=[
            case(
                "A1",
                "POST /api/v1/orders",
                body={"item_id": "S", "quantity": 0, "amount": 1.0, "remark": "x" * 130, "channel": "web"},
            ),
            case("A2", "GET /api/v1/orders", path="/api/v1/orders", query={"page_size": 51}),
            case(
                "A3",
                "PATCH /api/v1/orders/{order_id}/remark",
                path="/api/v1/orders/1/remark",
                body={"remark": "x"},
            ),
        ],
    )
    coverage = evaluate_change_coverage(suite, diff, new)
    assert coverage.covered == coverage.total == 5
    assert coverage.ratio == 1.0
    assert coverage.misses == []
