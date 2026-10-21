"""规模与性能测试。"""

from __future__ import annotations

import time

from benchmarks.synthetic import build_document
from app.parser import parse_spec_dict
from app.parser.normalize import CIRCULAR_MARKER
from app.parser.refs import RefResolver


def _contains_marker(node: object, depth: int = 0) -> bool:
    if depth > 60:
        return False
    if isinstance(node, dict):
        if CIRCULAR_MARKER in node:
            return True
        return any(_contains_marker(value, depth + 1) for value in node.values())
    if isinstance(node, list):
        return any(_contains_marker(item, depth + 1) for item in node)
    return False


def test_synthetic_document_is_deterministic() -> None:
    assert build_document(endpoints=60) == build_document(endpoints=60)


def test_large_spec_parses_completely() -> None:
    document = build_document(endpoints=200)
    expected = document["x-generated-operations"]
    spec = parse_spec_dict(document)

    assert len(spec.endpoints) == expected
    assert spec.title.startswith("Synthetic API")
    assert len({endpoint.uid for endpoint in spec.endpoints}) == expected

    create = spec.find("POST", "/api/v1/resource0")
    assert create is not None
    assert create.request_body is not None
    assert "status" in create.request_body.json_schema["properties"]
    assert create.request_body.json_schema["properties"]["status"]["enum"] == [
        "CREATED",
        "PAID",
        "CANCELLED",
    ]

    deep = spec.find("POST", "/api/v1/resource7")
    assert deep is not None and deep.request_body is not None
    assert "parent" in deep.request_body.json_schema["properties"]


def test_list_endpoint_allof_envelope_is_flattened() -> None:
    spec = parse_spec_dict(build_document(endpoints=20))
    listing = spec.find("GET", "/api/v1/resource0")
    assert listing is not None
    schema = listing.responses[0].json_schema
    assert {"page", "page_size", "total", "items"} <= set(schema["properties"])


def test_circular_reference_is_still_detected_at_scale() -> None:
    """Model7 的父链会绕回自己, 在 200 接口文档里也必须被识别成循环而不是爆栈。"""
    document = build_document(endpoints=200)
    resolved = RefResolver(document, max_depth=512).resolve(
        {"$ref": "#/components/schemas/Model7"}
    )
    assert _contains_marker(resolved)


def test_cache_hits_and_identical_results() -> None:
    document = build_document(endpoints=60)
    first = parse_spec_dict(document)
    second = parse_spec_dict(document)

    assert first.model_dump() == second.model_dump()

    resolver = RefResolver(document)
    resolver.resolve(document["paths"])
    assert resolver.cache_hits > 0


def test_repeated_ref_returns_shared_object() -> None:
    """这是缓存的实现代价: 同一个 $ref 返回同一对象, 因此结果必须视为只读。"""
    document = build_document(endpoints=10)
    resolver = RefResolver(document)
    first = resolver.resolve({"$ref": "#/components/schemas/Model0"})
    second = resolver.resolve({"$ref": "#/components/schemas/Model0"})
    assert first is second
    assert resolver.cache_hits == 1


def test_normalized_schemas_do_not_share_mutable_lists() -> None:
    """两条接口引用同一个模型时, 改其中一条的 enum 不能影响另一条。"""
    spec = parse_spec_dict(build_document(endpoints=200))
    first = spec.find("POST", "/api/v1/resource0")
    second = spec.find("POST", "/api/v1/resource40")
    assert first is not None and second is not None
    assert first.request_body is not None and second.request_body is not None

    first.request_body.json_schema["properties"]["status"]["enum"].append("HACKED")

    assert "HACKED" not in second.request_body.json_schema["properties"]["status"]["enum"]


def test_depth_guard_still_applies_with_cache() -> None:
    document = build_document(endpoints=20)
    resolver = RefResolver(document, max_depth=5)
    resolved = resolver.resolve({"$ref": "#/components/schemas/Model7"})
    assert isinstance(resolved, dict)


def test_parse_is_not_quadratic_in_practice() -> None:
    small = build_document(endpoints=100)
    large = build_document(endpoints=400)

    def elapsed(document: dict) -> float:
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            parse_spec_dict(document)
            best = min(best, time.perf_counter() - start)
        return best

    small_ms = elapsed(small) * 1000
    large_ms = elapsed(large) * 1000

    assert large_ms < small_ms * 8, f"疑似超线性退化: {small_ms:.0f}ms -> {large_ms:.0f}ms"
    assert large_ms < 5000, f"400 接口解析耗时 {large_ms:.0f}ms, 超出可用范围"
