"""把模型输出转换成可执行的用例, 顺手把幻觉拦掉。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from app.executor.models import Expectation, TestCase
from app.parser.models import EndpointSpec, ParamLocation

from .context import RuntimeContext, needs_credentials, needs_existing_resource
from .models import CaseCategory, GeneratedCase

STANDARD_HEADERS = frozenset({"content-type", "accept", "accept-encoding", "user-agent", "authorization"})

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_MAX_PRUNE_DEPTH = 12


@dataclass
class ConversionResult:
    case: TestCase | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)
    total_fields: int = 0

    @property
    def ok(self) -> bool:
        return self.case is not None


def flatten_paths(value: Any, prefix: str = "", *, max_depth: int = _MAX_PRUNE_DEPTH) -> list[str]:
    """把嵌套结构摊平成字段路径, 数组元素用 ``[]`` 表示。"""
    paths: list[str] = []
    if max_depth <= 0:
        return paths
    if isinstance(value, dict):
        for key, sub in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.append(path)
            paths.extend(flatten_paths(sub, path, max_depth=max_depth - 1))
    elif isinstance(value, list):
        for item in value:
            paths.extend(flatten_paths(item, f"{prefix}[]", max_depth=max_depth - 1))
    return paths


def prune_to_allowed(
    value: Any,
    allowed: set[str],
    *,
    prefix: str = "",
    dropped: list[str] | None = None,
    depth: int = 0,
) -> Any:
    """递归剔除不在白名单里的字段, 被剔除的路径记入 ``dropped``。"""
    if dropped is None:
        dropped = []
    if depth > _MAX_PRUNE_DEPTH:
        return value

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if path not in allowed:
                dropped.append(path)
                continue
            out[key] = prune_to_allowed(sub, allowed, prefix=path, dropped=dropped, depth=depth + 1)
        return out

    if isinstance(value, list):
        return [
            prune_to_allowed(item, allowed, prefix=f"{prefix}[]", dropped=dropped, depth=depth + 1)
            for item in value
        ]

    return value


def _resolve_path(template: str, path_params: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """填充 URL 占位符。返回 (path, 缺失的占位符, 多出来的参数名)。"""
    placeholders = _PLACEHOLDER_RE.findall(template)
    missing = [name for name in placeholders if name not in path_params]
    extra = [name for name in path_params if name not in placeholders]
    if missing:
        return template, missing, extra

    path = template
    for name in placeholders:
        path = path.replace("{" + name + "}", quote(str(path_params[name]), safe=""))
    return path, [], extra


def _default_status(category: CaseCategory) -> int:
    return 200 if category is CaseCategory.POSITIVE else 400


def convert_case(
    generated: GeneratedCase,
    endpoint: EndpointSpec,
    *,
    index: int,
    case_id_prefix: str = "AI",
    context: "RuntimeContext | None" = None,
) -> ConversionResult:
    """单条模型输出 -> :class:`TestCase`。失败时 ``case`` 为 None 并给出原因。"""
    result = ConversionResult()
    category = generated.normalized_category
    request = generated.request

    path, missing_path, extra_path = _resolve_path(endpoint.path, request.path_params or {})
    result.total_fields += len(request.path_params or {})
    result.dropped_fields.extend(f"path.{name}" for name in extra_path)
    if missing_path:
        result.errors.append(f"缺少路径参数: {', '.join(missing_path)}")
        return result

    allowed_query = {p.name for p in endpoint.params if p.location is ParamLocation.QUERY}
    query: dict[str, Any] = {}
    for key, value in (request.query or {}).items():
        result.total_fields += 1
        if key in allowed_query:
            query[key] = value
        else:
            result.dropped_fields.append(f"query.{key}")

    allowed_headers = {
        p.name.lower() for p in endpoint.params if p.location is ParamLocation.HEADER
    }
    headers: dict[str, str] = {}
    for key, value in (request.headers or {}).items():
        result.total_fields += 1
        lowered = str(key).lower()
        if lowered in allowed_headers or lowered in STANDARD_HEADERS:
            headers[str(key)] = str(value)
        else:
            result.dropped_fields.append(f"header.{key}")

    body = request.body
    if body is not None:
        if endpoint.request_body is None:
            result.dropped_fields.append("body")
            body = None
        else:
            allowed_body = endpoint.body_property_paths()
            result.total_fields += len(flatten_paths(body))
            if allowed_body:
                if not isinstance(body, dict):
                    result.errors.append("请求体必须是 JSON 对象")
                    return result
                before = len(result.dropped_fields)
                body = prune_to_allowed(body, allowed_body, dropped=result.dropped_fields)
                if len(result.dropped_fields) > before:
                    result.warnings.append(
                        "剔除幻觉字段: " + ", ".join(result.dropped_fields[before:])
                    )

            required = endpoint.request_body.json_schema.get("required") or []
            if required and isinstance(body, dict):
                absent = [name for name in required if name not in body]
                if absent:
                    result.warnings.append("缺少必填字段: " + ", ".join(absent))

    statuses = generated.expected.statuses
    if not statuses:
        fallback = _default_status(category)
        result.warnings.append(f"未给出 expected.status, 按 {fallback} 兜底")
        statuses = [fallback]
    if any(code >= 500 for code in statuses):
        result.warnings.append(f"期望状态码包含 5xx ({statuses}), 已改为 400")
        statuses = [400]

    if context is not None and needs_credentials(statuses):
        for name, value in context.headers.items():
            if headers.get(name, "") != value:
                headers[name] = value
                result.warnings.append(f"注入凭据 header: {name}")

    if context is not None and needs_existing_resource(statuses):
        for name, value in context.query.items():
            if name not in query and any(
                p.name == name and p.location is ParamLocation.QUERY for p in endpoint.params
            ):
                query[name] = value
                result.warnings.append(f"注入查询参数: {name}={value}")

        placeholders = set(_PLACEHOLDER_RE.findall(endpoint.path))
        matched = {k: v for k, v in context.path_params.items() if k in placeholders}
        if matched:
            merged = {**(request.path_params or {}), **matched}
            new_path, still_missing, _ = _resolve_path(endpoint.path, merged)
            if not still_missing and new_path != path:
                path = new_path
                result.warnings.append(
                    "注入已知存在的路径参数: "
                    + ", ".join(f"{k}={v}" for k, v in matched.items())
                )

    expectation = Expectation(status=statuses[0] if len(statuses) == 1 else statuses)
    tags = [category.value]
    if category is CaseCategory.POSITIVE and "positive" not in tags:
        tags.append("positive")

    result.case = TestCase(
        id=f"{case_id_prefix}-{index:03d}",
        name=generated.case_name,
        endpoint=endpoint.uid,
        method=endpoint.method,
        path=path,
        query=query,
        headers=headers,
        body=body,
        expect=expectation,
        tags=tags,
        description=generated.scenario or generated.expected.note,
    )
    return result
