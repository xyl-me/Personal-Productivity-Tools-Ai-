"""M2 端到端: mock 生成 -> 校验 -> 保存 -> 真实执行 -> 缺陷归因。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.executor.ground_truth import GROUND_TRUTH
from app.executor.live import live_server
from app.executor.runner import run_cases
from app.executor.triage import evaluate
from app.generator.cache import GenerationCache
from app.generator.client import build_client
from app.generator.engine import run_generation, to_suite
from app.parser import parse_spec_dict
from target_service.main import app, reset_demo_data

SPEC = parse_spec_dict(app.openapi())
MOCK_ENDPOINTS = ["POST /api/v1/orders", "GET /api/v1/orders"]


@pytest.fixture(scope="module")
def generation():
    return run_generation(
        SPEC, build_client(mode="mock"), endpoints=MOCK_ENDPOINTS, cache=None
    )


@pytest.fixture(scope="module")
def suite(generation):
    return to_suite(generation, name="ai_mock")


def test_mock_pipeline_produces_executable_cases(generation, suite) -> None:
    assert generation.mode == "mock"
    assert generation.total_cases == 8
    assert len(suite) == 8
    assert all(case.id.startswith("AI-") for case in suite.cases)


def test_hallucinated_fields_are_stripped(generation) -> None:
    """fixtures 里故意塞了 3 个编造字段, 必须一个不剩地被拦掉。"""
    assert set(generation.dropped_fields) == {
        "query.trace_id",
        "header.X-Request-Id",
        "coupon_code",
    }


def test_missing_required_field_is_only_a_warning(generation) -> None:
    """必填缺失本身就是一种合法用例, 不能当成"生成失败"丢弃。"""
    warnings = [warning for endpoint in generation.endpoints for warning in endpoint.warnings]
    assert any("缺少必填字段" in warning for warning in warnings)


def test_pipeline_finds_seeded_defects(generation, suite) -> None:
    with live_server(app) as base_url:
        report = run_cases(suite, base_url=base_url, reset=reset_demo_data)

    evaluation = evaluate(suite, report, GROUND_TRUTH)
    hit = {defect_id for defect_id, ids in evaluation.hits.items() if ids}
    assert hit == {"D1", "D2", "D5"}
    assert evaluation.detected == 3
    assert evaluation.duplicate_hits == 1
    assert evaluation.unattributed == []
    assert evaluation.false_positives == []


def test_generation_is_cacheable(tmp_dir: Path) -> None:
    client = build_client(mode="mock")
    cache = GenerationCache(tmp_dir / "cache.db")

    first = run_generation(SPEC, client, endpoints=MOCK_ENDPOINTS, cache=cache)
    second = run_generation(SPEC, client, endpoints=MOCK_ENDPOINTS, cache=cache)

    assert first.total_cases == second.total_cases
    assert second.cache_hits == 2
    assert len(cache) == 2


def test_generated_suite_can_be_round_tripped(tmp_dir: Path) -> None:
    """保存再读回来的用例必须完全等价 (页面下载的 JSON 和内存里的对象是同一份)。"""
    from app.executor.runner import load_case_suite, save_case_suite

    generation_run = run_generation(SPEC, build_client(mode="mock"), endpoints=MOCK_ENDPOINTS)
    path = save_case_suite(to_suite(generation_run, name="rt"), tmp_dir / "cases.json")
    reloaded = load_case_suite(path)
    assert [case.model_dump() for case in reloaded.cases] == [
        case.model_dump() for case in to_suite(generation_run, name="rt").cases
    ]
