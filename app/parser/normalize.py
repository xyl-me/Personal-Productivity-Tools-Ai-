"""JSON Schema 规范化。"""

from __future__ import annotations

import copy
from typing import Any

from .refs import CIRCULAR_MARKER

_STRICTER_LOWER = frozenset(
    {"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties"}
)
_STRICTER_UPPER = frozenset(
    {"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"}
)
_COMBINATOR_KEYS = ("anyOf", "oneOf")

SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "enum",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "multipleOf",
        "items",
        "properties",
        "required",
        "nullable",
        "example",
        "description",
        "title",
        "additionalProperties",
        "oneOf",
        "anyOf",
        "allOf",
    }
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def merge_schemas(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """按 ``allOf`` 的语义合并多份 schema。"""
    result: dict[str, Any] = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        for key, value in part.items():
            if key == "properties" and isinstance(value, dict):
                props = result.setdefault("properties", {})
                for name, sub in value.items():
                    prev = props.get(name)
                    if isinstance(prev, dict) and isinstance(sub, dict) and prev is not sub:
                        props[name] = merge_schemas([prev, sub])
                    else:
                        props[name] = sub
            elif key == "required" and isinstance(value, list):
                required = result.setdefault("required", [])
                for item in value:
                    if item not in required:
                        required.append(item)
            elif key in _STRICTER_LOWER and _is_number(value):
                current = result.get(key)
                result[key] = value if not _is_number(current) else max(current, value)
            elif key in _STRICTER_UPPER and _is_number(value):
                current = result.get(key)
                result[key] = value if not _is_number(current) else min(current, value)
            elif key == "enum" and isinstance(value, list):
                current = result.get("enum")
                result["enum"] = list(value) if current is None else [v for v in current if v in value]
            elif key == "type":
                current = result.get("type")
                if current is None:
                    result["type"] = value
                elif current != value:
                    conflicts = result.setdefault("x-conflicting-types", [])
                    for candidate in (current, value):
                        if isinstance(candidate, str) and candidate not in conflicts:
                            conflicts.append(candidate)
            else:
                result[key] = value
    return result


def merge_all_of(schema: dict[str, Any]) -> dict[str, Any]:
    """把 ``allOf`` 拍扁成单个 schema (递归处理嵌套 allOf)。"""
    branches = schema.get("allOf")
    if not isinstance(branches, list):
        return dict(schema)
    base = {key: value for key, value in schema.items() if key != "allOf"}
    parts = [merge_all_of(branch) for branch in branches if isinstance(branch, dict)]
    parts.append(base)
    return merge_schemas(parts)


def _is_null_branch(branch: Any) -> bool:
    if not isinstance(branch, dict):
        return False
    if "x-circular-ref" in branch:
        return False
    kind = branch.get("type")
    if kind == "null":
        return True
    return isinstance(kind, list) and kind == ["null"]


def collapse_nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """把 ``anyOf/oneOf`` 或数组式 ``type`` 表示的可空性, 折叠成 ``nullable: true``。"""
    out = dict(schema)

    kind = out.get("type")
    if isinstance(kind, list):
        non_null = [item for item in kind if item != "null"]
        if len(non_null) != len(kind):
            out["nullable"] = True
        if len(non_null) == 1:
            out["type"] = non_null[0]
        elif non_null:
            out["type"] = non_null

    for key in _COMBINATOR_KEYS:
        branches = out.get(key)
        if not isinstance(branches, list) or len(branches) < 2:
            continue
        non_null = [branch for branch in branches if not _is_null_branch(branch)]
        if len(non_null) == 1 and len(non_null) == len(branches) - 1 and isinstance(non_null[0], dict):
            rest = {k: v for k, v in out.items() if k != key}
            merged = merge_schemas([rest, non_null[0]])
            merged["nullable"] = True
            out = merged
            break

    return out


def normalize_schema(schema: Any, *, max_depth: int = 32, _depth: int = 0) -> dict[str, Any]:
    """把一份已展开 ``$ref`` 的 schema 规范化成扁平、可读的结构。"""
    if not isinstance(schema, dict) or _depth > max_depth:
        return {}
    if CIRCULAR_MARKER in schema:
        return copy.deepcopy(schema)

    out = collapse_nullable(merge_all_of(schema))
    out = dict(out)

    props = out.get("properties")
    if isinstance(props, dict):
        out["properties"] = {
            name: normalize_schema(sub, max_depth=max_depth, _depth=_depth + 1)
            for name, sub in props.items()
        }

    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = normalize_schema(items, max_depth=max_depth, _depth=_depth + 1)
    elif isinstance(items, list):
        out["items"] = [normalize_schema(sub, max_depth=max_depth, _depth=_depth + 1) for sub in items]

    for key in _COMBINATOR_KEYS:
        branches = out.get(key)
        if isinstance(branches, list):
            out[key] = [
                normalize_schema(branch, max_depth=max_depth, _depth=_depth + 1) for branch in branches
            ]

    additional = out.get("additionalProperties")
    if isinstance(additional, dict):
        out["additionalProperties"] = normalize_schema(
            additional, max_depth=max_depth, _depth=_depth + 1
        )

    for key in ("enum", "required"):
        if isinstance(out.get(key), list):
            out[key] = list(out[key])

    return out


def collect_property_paths(
    schema: Any, prefix: str = "", *, max_depth: int = 8, _depth: int = 0
) -> set[str]:
    """收集 schema 里所有可直接使用的字段路径。"""
    paths: set[str] = set()
    if not isinstance(schema, dict) or _depth >= max_depth:
        return paths
    if CIRCULAR_MARKER in schema:
        return paths

    schema = merge_all_of(schema)
    if CIRCULAR_MARKER in schema:
        return paths

    props = schema.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            path = f"{prefix}.{name}" if prefix else name
            paths.add(path)
            paths |= collect_property_paths(sub, path, max_depth=max_depth, _depth=_depth + 1)

    items = schema.get("items")
    if isinstance(items, dict):
        item_prefix = f"{prefix}[]" if prefix else "[]"
        paths |= collect_property_paths(items, item_prefix, max_depth=max_depth, _depth=_depth + 1)

    for key in _COMBINATOR_KEYS:
        for branch in schema.get(key) or []:
            paths |= collect_property_paths(
                branch, prefix, max_depth=max_depth, _depth=_depth + 1
            )

    return paths
