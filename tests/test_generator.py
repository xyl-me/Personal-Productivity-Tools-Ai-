"""生成层单元测试: Prompt、解析、校验、重试、缓存。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable

import pytest

from app.generator.cache import GenerationCache
from app.generator.client import (
    MockClient,
    OutputTruncated,
    build_client,
    describe_connection_hint,
    describe_error,
    endpoint_from_prompt,
    prompt_key,
)
from app.generator.engine import (
    ParseError,
    generate_all,
    generate_endpoint,
    parse_batch,
    run_generation,
    to_suite,
)
from app.generator.models import GeneratedCase, LLMResponse
from app.generator.prompts import build_retry_prompt, build_user_prompt
from app.generator.validate import convert_case, flatten_paths, prune_to_allowed
from app.parser import parse_spec_dict
from target_service.main import app

SPEC = parse_spec_dict(app.openapi())
POST_ORDERS = "POST /api/v1/orders"
GET_ORDERS = "GET /api/v1/orders"


def endpoint(uid: str):
    found = SPEC.endpoint_index().get(uid)
    assert found is not None, f"缺少接口 {uid}"
    return found


Creator = Callable[[str, int], "str | Exception"]


class ScriptedClient:
    """按顺序吐出预设响应的假客户端。"""

    mode = "real"
    model = "scripted"

    def __init__(self, responder: Creator) -> None:
        self._responder = responder
        self.calls = 0

    async def complete(self, messages, *, temperature: float, max_tokens: int) -> LLMResponse:
        self.calls += 1
        prompt = messages[-1]["content"]
        out = self._responder(prompt, self.calls)
        if isinstance(out, Exception):
            raise out
        return LLMResponse(text=out, model=self.model, prompt_tokens=10, completion_tokens=20)


def good_json(prompt: str) -> str:
    """按接口形状返回一条合法正向用例, 保证每个接口都至少能产出一条。"""
    uid = endpoint_from_prompt(prompt) or ""
    method = uid.split(" ")[0] if uid else "GET"
    request: dict[str, object] = {"path_params": {"order_id": "1"} if "{order_id}" in uid else {}}

    if method == "POST":
        request["body"] = {"item_id": "SKU-1", "quantity": 1, "amount": 1.0}
        status = 201
    elif method == "PUT":
        request["body"] = {"status": "PAID"}
        status = 200
    else:
        if "search" in uid:
            request["query"] = {"q": "SKU-1002"}
        elif method == "GET":
            request["query"] = {"page": 1, "page_size": 10}
        status = 200

    cases = [
        {
            "case_name": f"{method} 正向",
            "category": "positive",
            "type": "正向",
            "request": request,
            "expected": {"status": status},
        }
    ]
    return json.dumps({"cases": cases}, ensure_ascii=False)


def make_generated(**overrides) -> GeneratedCase:
    payload = {
        "case_name": "建单",
        "category": "positive",
        "type": "正向",
        "request": {"body": {"item_id": "SKU-1", "quantity": 1, "amount": 1.0}},
        "expected": {"status": 201},
    }
    payload.update(overrides)
    return GeneratedCase.model_validate(payload)


def test_prompt_carries_legal_field_list() -> None:
    text = build_user_prompt(endpoint(POST_ORDERS), count=5)
    for field in ("item_id", "quantity", "amount", "remark"):
        assert field in text
    assert "## POST /api/v1/orders" in text
    assert "合法字段清单" in text
    assert "5 条用例" in text


def test_retry_prompt_includes_error_and_preview() -> None:
    text = build_retry_prompt("base", error="JSON 解析失败: x", bad_output="oops", count=3)
    assert "JSON 解析失败" in text
    assert "oops" in text
    assert "3 条" in text


def test_endpoint_is_recoverable_from_prompt() -> None:
    assert endpoint_from_prompt(build_user_prompt(endpoint(GET_ORDERS), count=3)) == GET_ORDERS


def test_parse_batch_accepts_plain_object() -> None:
    batch = parse_batch(good_json("## POST /x"))
    assert len(batch) == 1
    assert batch.cases[0].case_name == "POST 正向"


def test_parse_batch_strips_code_fence() -> None:
    text = "```json\n" + good_json("## POST /x") + "\n```"
    assert len(parse_batch(text)) == 1


def test_parse_batch_recovers_from_surrounding_prose() -> None:
    text = "以下是生成的用例:\n" + good_json("## POST /x") + "\n希望有帮助。"
    assert len(parse_batch(text)) == 1


def test_parse_batch_accepts_bare_list() -> None:
    batch = parse_batch('[{"case_name":"a","request":{},"expected":{"status":200}}]')
    assert batch.cases[0].case_name == "a"


def test_parse_batch_accepts_single_case_object() -> None:
    batch = parse_batch('{"case_name":"a","request":{},"expected":{"status":200}}')
    assert len(batch) == 1


@pytest.mark.parametrize(
    "text",
    ["", "   ", "not json at all", '{"cases": []}', '{"foo": 1}', "[1, 2, 3]"],
)
def test_parse_batch_rejects_bad_output(text: str) -> None:
    with pytest.raises(ParseError):
        parse_batch(text)


def test_convert_replaces_path_placeholder() -> None:
    gen = make_generated(
        request={"path_params": {"order_id": "42"}},
        expected={"status": 200},
    )
    result = convert_case(gen, endpoint("GET /api/v1/orders/{order_id}"), index=1)
    assert result.case is not None
    assert result.case.path == "/api/v1/orders/42"
    assert result.case.endpoint == "GET /api/v1/orders/{order_id}"


def test_convert_requires_every_path_placeholder() -> None:
    gen = make_generated(request={"path_params": {}}, expected={"status": 200})
    result = convert_case(gen, endpoint("GET /api/v1/orders/{order_id}"), index=1)
    assert result.case is None
    assert any("缺少路径参数" in error for error in result.errors)


def test_convert_drops_hallucinated_query_and_body_fields() -> None:
    """POST /orders 没有 query 参数, 所以 page 也算幻觉字段 —— 位置不对同样是编造。"""
    gen = make_generated(
        request={
            "query": {"page": 1, "trace_id": "x"},
            "body": {"item_id": "SKU-1", "quantity": 1, "amount": 1.0, "coupon_code": "C"},
        }
    )
    result = convert_case(gen, endpoint(POST_ORDERS), index=1)
    assert result.case is not None
    assert result.case.query == {}
    assert result.case.body == {"item_id": "SKU-1", "quantity": 1, "amount": 1.0}
    assert set(result.dropped_fields) == {"query.page", "query.trace_id", "coupon_code"}


def test_convert_keeps_standard_headers_drops_unknown() -> None:
    gen = make_generated(
        request={
            "headers": {"Content-Type": "application/json", "X-Trace-Id": "t"},
            "body": {"item_id": "SKU-1", "quantity": 1, "amount": 1.0},
        }
    )
    result = convert_case(gen, endpoint(POST_ORDERS), index=1)
    assert result.case is not None
    assert "Content-Type" in result.case.headers
    assert "X-Trace-Id" not in result.case.headers
    assert "header.X-Trace-Id" in result.dropped_fields


def test_convert_keeps_declared_header() -> None:
    gen = make_generated(
        request={"headers": {"X-Admin-Token": "t"}}, expected={"status": 200}
    )
    result = convert_case(gen, endpoint("GET /api/v1/admin/orders/export"), index=1)
    assert result.case is not None
    assert result.case.headers == {"X-Admin-Token": "t"}


def test_convert_drops_body_when_endpoint_has_none() -> None:
    gen = make_generated(request={"body": {"a": 1}}, expected={"status": 200})
    result = convert_case(gen, endpoint(GET_ORDERS), index=1)
    assert result.case is not None
    assert result.case.body is None
    assert "body" in result.dropped_fields


def test_convert_defaults_missing_status_by_category() -> None:
    positive = convert_case(make_generated(expected={}), endpoint(POST_ORDERS), index=1)
    negative = convert_case(
        make_generated(category="boundary", type="异常", expected={}), endpoint(POST_ORDERS), index=1
    )
    assert positive.case is not None and positive.case.expect.statuses == [200]
    assert negative.case is not None and negative.case.expect.statuses == [400]
    assert any("兜底" in warning for warning in negative.warnings)


def test_convert_refuses_5xx_as_expected_result() -> None:
    result = convert_case(make_generated(expected={"status": 500}), endpoint(POST_ORDERS), index=1)
    assert result.case is not None
    assert result.case.expect.statuses == [400]
    assert any("5xx" in warning for warning in result.warnings)


def test_convert_tags_positive_cases() -> None:
    result = convert_case(make_generated(), endpoint(POST_ORDERS), index=1)
    assert result.case is not None
    assert "positive" in result.case.tags


def test_convert_records_missing_required_field_as_warning() -> None:
    gen = make_generated(request={"body": {"item_id": "SKU-1"}})
    result = convert_case(gen, endpoint(POST_ORDERS), index=1)
    assert result.case is not None
    assert any("缺少必填字段" in warning for warning in result.warnings)


def test_convert_drops_extra_path_params() -> None:
    gen = make_generated(
        request={"path_params": {"order_id": "1", "tenant": "a"}}, expected={"status": 200}
    )
    result = convert_case(gen, endpoint("GET /api/v1/orders/{order_id}"), index=1)
    assert "path.tenant" in result.dropped_fields


def test_convert_quotes_path_values() -> None:
    gen = make_generated(
        request={"path_params": {"order_id": "a b"}}, expected={"status": 404}
    )
    result = convert_case(gen, endpoint("GET /api/v1/orders/{order_id}"), index=1)
    assert result.case is not None
    assert result.case.path == "/api/v1/orders/a%20b"


def test_convert_rejects_non_object_body() -> None:
    gen = make_generated(request={"body": [1, 2, 3]})
    result = convert_case(gen, endpoint(POST_ORDERS), index=1)
    assert result.case is None
    assert any("JSON 对象" in error for error in result.errors)


def test_flatten_paths_handles_arrays() -> None:
    assert flatten_paths({"items": [{"sku": "a"}]}) == ["items", "items[].sku"]


def test_prune_to_allowed_removes_nested_unknown() -> None:
    dropped: list[str] = []
    pruned = prune_to_allowed({"a": 1, "b": {"c": 2, "d": 3}}, {"a", "b", "b.c"}, dropped=dropped)
    assert pruned == {"a": 1, "b": {"c": 2}}
    assert dropped == ["b.d"]


def test_array_body_field_whitelist() -> None:
    """数组元素里的字段也要能被白名单识别 (items[].sku)。"""
    spec = parse_spec_dict(
        {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/batch": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "items": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {"sku": {"type": "string"}},
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    target = spec.endpoints[0]
    gen = make_generated(
        request={"body": {"items": [{"sku": "A", "weird": 1}]}}, expected={"status": 200}
    )
    result = convert_case(gen, target, index=1)
    assert result.case is not None
    assert result.case.body == {"items": [{"sku": "A"}]}
    assert result.dropped_fields == ["items[].weird"]


def test_generate_endpoint_accepts_cases() -> None:
    client = ScriptedClient(lambda prompt, n: good_json(prompt))
    record = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client))
    assert record.accepted_count == 1
    assert record.attempts == 1
    assert record.accepted[0].endpoint == GET_ORDERS


def test_retry_with_error_feedback_succeeds() -> None:
    def responder(prompt: str, call: int) -> str:
        return "不是 JSON" if call == 1 else good_json(prompt)

    client = ScriptedClient(responder)
    record = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client, max_retries=2))
    assert client.calls == 2
    assert record.attempts == 2
    assert record.accepted_count == 1
    assert len(record.parse_errors) == 1


def test_retry_exhausted_records_error() -> None:
    client = ScriptedClient(lambda prompt, n: "永远不是 JSON")
    record = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client, max_retries=1))
    assert client.calls == 2
    assert record.accepted_count == 0
    assert len(record.parse_errors) == 2


def test_truncation_retries_with_fewer_cases() -> None:
    """截断不能用同一个 prompt 重试 —— 必须减少条数, 否则每次都在同一处被切掉。"""
    seen: list[str] = []

    def responder(prompt: str, call: int):
        seen.append(prompt)
        if call == 1:
            return OutputTruncated("max_tokens 打满", completion_tokens=8192)
        return good_json(prompt)

    client = ScriptedClient(responder)
    record = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client, count=6, max_retries=2))

    assert record.accepted_count == 1
    assert record.attempts == 2
    assert any("截断" in item for item in record.parse_errors)
    assert record.completion_tokens >= 8192
    assert "3 条" in seen[1]
    assert "被截断" in seen[1]


def test_truncation_gives_up_after_retries() -> None:
    client = ScriptedClient(lambda prompt, n: OutputTruncated("又截断了", completion_tokens=100))
    record = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client, count=6, max_retries=1))
    assert record.accepted_count == 0
    assert len(record.parse_errors) == 2


def test_describe_error_walks_cause_chain() -> None:
    inner = ConnectionRefusedError("refused")
    outer = RuntimeError("Connection error.")
    outer.__cause__ = inner
    text = describe_error(outer)
    assert "RuntimeError" in text and "ConnectionRefusedError" in text


@pytest.mark.parametrize(
    ("exc", "keyword"),
    [
        (OSError("certificate verify failed"), "证书"),
        (OSError("proxy connect failed"), "代理"),
        (TimeoutError("timed out"), "超时"),
    ],
)
def test_describe_connection_hint(exc: Exception, keyword: str) -> None:
    hint = describe_connection_hint(exc)
    assert hint is not None and keyword in hint


def test_duplicate_cases_are_rejected() -> None:
    case = {
        "case_name": "重复",
        "category": "positive",
        "type": "正向",
        "request": {"query": {"page": 1}},
        "expected": {"status": 200},
    }
    client = ScriptedClient(lambda prompt, n: json.dumps({"cases": [case, case]}, ensure_ascii=False))
    record = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client))
    assert record.accepted_count == 1
    assert any("重复" in item for item in record.rejected)


def test_cache_prevents_second_model_call(tmp_dir: Path) -> None:
    cache = GenerationCache(tmp_dir / "cache.db")
    client = ScriptedClient(lambda prompt, n: good_json(prompt))

    first = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client, cache=cache))
    calls_after_first = client.calls
    second = asyncio.run(generate_endpoint(endpoint(GET_ORDERS), client, cache=cache))

    assert first.accepted_count == second.accepted_count == 1
    assert second.cached is True
    assert client.calls == calls_after_first
    assert len(cache) == 1


def test_cache_key_sensitive_to_model_and_temperature() -> None:
    base = prompt_key("m", 0.1, "p")
    assert base == prompt_key("m", 0.1, "p")
    assert base != prompt_key("m", 0.2, "p")
    assert base != prompt_key("m2", 0.1, "p")
    assert base != prompt_key("m", 0.1, "p2")


def test_generate_all_covers_every_endpoint() -> None:
    client = ScriptedClient(lambda prompt, n: good_json(prompt))
    run = asyncio.run(generate_all(SPEC, client, max_concurrency=3))
    assert len(run.endpoints) == 7
    assert run.total_cases == 7
    assert run.mode == "real"


def test_generate_all_failure_is_isolated() -> None:
    """单个接口炸掉不能拖垮整轮生成。"""

    def responder(prompt: str, call: int):
        if "search" in prompt:
            raise RuntimeError("boom")
        return good_json(prompt)

    client = ScriptedClient(responder)
    run = asyncio.run(generate_all(SPEC, client, max_concurrency=2))
    broken = [ep for ep in run.endpoints if ep.parse_errors and not ep.accepted]
    assert len(broken) == 1
    assert "boom" in broken[0].parse_errors[0]
    assert run.total_cases > 0


def test_to_suite_collects_all_cases() -> None:
    client = ScriptedClient(lambda prompt, n: good_json(prompt))
    run = asyncio.run(generate_all(SPEC, client))
    suite = to_suite(run, name="ai")
    assert suite.suite == "ai"
    assert len(suite) == run.total_cases
    assert all(case.id.startswith("AI-") for case in suite.cases)


def test_run_generation_is_sync_entrypoint() -> None:
    client = ScriptedClient(lambda prompt, n: good_json(prompt))
    run = run_generation(SPEC, client, endpoints=[GET_ORDERS])
    assert len(run.endpoints) == 1
    assert run.total_cases == 1


def test_build_client_prefers_mock_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert build_client(mode="auto").mode == "mock"


def test_build_client_requires_key_in_real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        build_client(mode="real")


def test_mock_client_reports_missing_fixture() -> None:
    client = MockClient({"GET /only": {"cases": []}})
    with pytest.raises(Exception):
        asyncio.run(client.complete([{"role": "user", "content": "## POST /other\n"}], temperature=0.1, max_tokens=10))
