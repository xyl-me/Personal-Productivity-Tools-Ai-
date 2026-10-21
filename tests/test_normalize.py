"""Schema 规范化测试: allOf 合并语义、可空折叠、字段路径收集。"""

from __future__ import annotations

from app.parser.normalize import (
    collapse_nullable,
    collect_property_paths,
    merge_all_of,
    normalize_schema,
)


def test_merge_all_of_flattens_properties_and_required() -> None:
    schema = {
        "allOf": [
            {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        ]
    }
    merged = merge_all_of(schema)
    assert set(merged["properties"]) == {"id", "name"}
    assert merged["required"] == ["id", "name"]
    assert "allOf" not in merged


def test_merge_all_of_takes_strictest_bounds() -> None:
    """allOf 的语义是交集: 下界取最大、上界取最小, 否则约束会被放宽。"""
    merged = merge_all_of(
        {
            "allOf": [
                {"type": "integer", "minimum": 1, "maximum": 100},
                {"minimum": 5, "maxLength": 10},
                {"maximum": 20, "maxLength": 20},
            ]
        }
    )
    assert merged["minimum"] == 5
    assert merged["maximum"] == 20
    assert merged["maxLength"] == 10


def test_merge_all_of_intersects_enum() -> None:
    merged = merge_all_of({"allOf": [{"enum": ["a", "b", "c"]}, {"enum": ["b", "c", "d"]}]})
    assert merged["enum"] == ["b", "c"]


def test_merge_all_of_deep_merges_same_property() -> None:
    merged = merge_all_of(
        {
            "allOf": [
                {"properties": {"amount": {"type": "number", "minimum": 0}}},
                {"properties": {"amount": {"maximum": 999}}},
            ]
        }
    )
    assert merged["properties"]["amount"] == {"type": "number", "minimum": 0, "maximum": 999}


def test_merge_all_of_records_type_conflict() -> None:
    merged = merge_all_of({"allOf": [{"type": "string"}, {"type": "integer"}]})
    assert merged["type"] == "string"
    assert merged["x-conflicting-types"] == ["string", "integer"]


def test_merge_all_of_is_noop_without_all_of() -> None:
    schema = {"type": "string", "maxLength": 3}
    assert merge_all_of(schema) == schema


def test_collapse_anyof_null() -> None:
    collapsed = collapse_nullable(
        {"anyOf": [{"type": "string", "maxLength": 8}, {"type": "null"}]}
    )
    assert collapsed["type"] == "string"
    assert collapsed["maxLength"] == 8
    assert collapsed["nullable"] is True
    assert "anyOf" not in collapsed


def test_collapse_type_list_form() -> None:
    collapsed = collapse_nullable({"type": ["integer", "null"]})
    assert collapsed == {"type": "integer", "nullable": True}


def test_real_union_is_preserved() -> None:
    """两个非空分支是真联合, 不能被折叠成可空单类型。"""
    schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    assert collapse_nullable(schema) == schema


def test_nullable_keyword_is_kept() -> None:
    assert collapse_nullable({"type": "string", "nullable": True})["nullable"] is True


def test_normalize_schema_recurses_into_nested_structures() -> None:
    schema = normalize_schema(
        {
            "allOf": [
                {"type": "object", "properties": {"id": {"type": "string"}}},
                {
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "allOf": [
                                    {"type": "object"},
                                    {"properties": {"sku": {"type": "string"}}},
                                ]
                            },
                        }
                    }
                },
            ]
        }
    )
    assert schema["properties"]["id"]["type"] == "string"
    assert schema["properties"]["items"]["items"]["properties"]["sku"]["type"] == "string"


def test_normalize_schema_keeps_circular_marker() -> None:
    marker = {"x-circular-ref": "#/components/schemas/Node", "type": "object"}
    assert normalize_schema(marker) == marker


def test_normalize_schema_handles_non_dict() -> None:
    assert normalize_schema(None) == {}
    assert normalize_schema("oops") == {}


def test_collect_property_paths_nested_object() -> None:
    schema = {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "address": {
                    "type": "object", "properties": {"city": {"type": "string"}}
                }},
            }
        },
    }
    assert collect_property_paths(schema) == {"user", "user.name", "user.address", "user.address.city"}


def test_collect_property_paths_array_items() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {"sku": {"type": "string"}}}}
        },
    }
    assert collect_property_paths(schema) == {"items", "items[].sku"}


def test_collect_property_paths_stops_at_circular_ref() -> None:
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "parent": {"x-circular-ref": "#/components/schemas/Node", "type": "object"},
        },
    }
    assert collect_property_paths(schema) == {"id", "parent"}
