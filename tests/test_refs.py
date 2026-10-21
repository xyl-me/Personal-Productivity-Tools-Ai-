"""$ref 解析的单元测试。"""

from __future__ import annotations

import copy

import pytest

from app.parser import CIRCULAR_MARKER, RefResolver, resolve_pointer
from app.parser.errors import UnresolvedRefError

DOCUMENT = {
    "components": {
        "schemas": {
            "Node": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "parent": {"$ref": "#/components/schemas/Node"},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Node"},
                    },
                },
            },
            "A": {"type": "object", "properties": {"b": {"$ref": "#/components/schemas/B"}}},
            "B": {"type": "object", "properties": {"a": {"$ref": "#/components/schemas/A"}}},
            "Base": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "WithDescription": {
                "$ref": "#/components/schemas/Base",
                "description": "覆盖后的描述",
            },
            "Plain": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
    }
}


def test_pointer_resolves_plain_path() -> None:
    node = resolve_pointer(DOCUMENT, "#/components/schemas/Plain")
    assert node["properties"]["name"]["type"] == "string"


def test_pointer_unescapes_slash_and_tilde() -> None:
    document = {"paths": {"/api/v1/orders": {"get": {"summary": "list"}}}, "a~b": {"c": 1}}
    assert resolve_pointer(document, "#/paths/~1api~1v1~1orders/get")["summary"] == "list"
    assert resolve_pointer(document, "#/a~0b/c") == 1


def test_pointer_missing_key_raises() -> None:
    with pytest.raises(UnresolvedRefError):
        resolve_pointer(DOCUMENT, "#/components/schemas/Nope")


def test_external_ref_is_rejected() -> None:
    with pytest.raises(UnresolvedRefError):
        resolve_pointer(DOCUMENT, "other.json#/components/schemas/Node")


def test_self_reference_is_truncated_not_recursive() -> None:
    resolved = RefResolver(DOCUMENT).resolve({"$ref": "#/components/schemas/Node"})

    parent = resolved["properties"]["parent"]
    assert parent[CIRCULAR_MARKER] == "#/components/schemas/Node"
    assert parent["type"] == "object"

    items = resolved["properties"]["children"]["items"]
    assert items[CIRCULAR_MARKER] == "#/components/schemas/Node"


def test_mutual_reference_terminates() -> None:
    resolved = RefResolver(DOCUMENT).resolve({"$ref": "#/components/schemas/A"})
    assert resolved["properties"]["b"]["properties"]["a"][CIRCULAR_MARKER] == (
        "#/components/schemas/A"
    )


def test_non_circular_branch_is_fully_expanded() -> None:
    """同一份文档里, 有环的分支截断, 无环的分支必须完整展开。"""
    resolved = RefResolver(DOCUMENT).resolve(
        {
            "type": "object",
            "properties": {
                "plain": {"$ref": "#/components/schemas/Plain"},
                "node": {"$ref": "#/components/schemas/Node"},
            },
        }
    )
    assert resolved["properties"]["plain"]["properties"]["name"]["type"] == "string"
    assert CIRCULAR_MARKER in resolved["properties"]["node"]["properties"]["parent"]


def test_ref_sibling_fields_override_target() -> None:
    resolved = RefResolver(DOCUMENT).resolve({"$ref": "#/components/schemas/WithDescription"})
    assert resolved["description"] == "覆盖后的描述"
    assert resolved["properties"]["id"]["type"] == "string"


def test_max_depth_guard_prevents_runaway_expansion() -> None:
    """无环但极深的结构也要有兜底, 否则 80 层嵌套配上大文档就能拖垮服务。"""
    deep: dict = {"type": "object", "properties": {}}
    cursor = deep
    for _ in range(80):
        child: dict = {"type": "object", "properties": {}}
        cursor["properties"]["child"] = child
        cursor = child

    resolved = RefResolver({"deep": deep}, max_depth=10).resolve({"$ref": "#/deep"})

    node: object = resolved
    seen_marker = False
    for _ in range(40):
        if not isinstance(node, dict):
            break
        if node.get("x-depth-truncated") is True:
            seen_marker = True
            break
        node = (node.get("properties") or {}).get("child")
    assert seen_marker


def test_resolve_does_not_mutate_document() -> None:
    """解析必须产出新对象: 同一份 document 会被反复解析 (缓存/重试), 不能越解析越"胖"。"""
    snapshot = copy.deepcopy(DOCUMENT)
    RefResolver(DOCUMENT).resolve({"$ref": "#/components/schemas/Node"})
    assert DOCUMENT == snapshot
