"""从 OpenAPI 3.x / Swagger 2.0 文档中提取接口清单。"""

from __future__ import annotations

from typing import Any

from .errors import MalformedSpecError
from .models import (
    ApiSpec,
    EndpointSpec,
    MediaType,
    ParamLocation,
    ParamSpec,
    RequestBodySpec,
    ResponseSpec,
)
from .normalize import SCHEMA_KEYS, normalize_schema
from .refs import RefResolver, ref_name, resolve_pointer

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
METHOD_ORDER = {method: index for index, method in enumerate(HTTP_METHODS)}


def _deref_top(raw: Any, document: dict[str, Any]) -> dict[str, Any]:
    """只跟随**顶层** ``$ref``, 保留内部的 $ref 以便推断结构名。"""
    node = raw
    seen: set[str] = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if ref in seen:
            break
        seen.add(ref)
        node = resolve_pointer(document, ref)
    return node if isinstance(node, dict) else {}


def _to_location(value: Any) -> ParamLocation:
    if isinstance(value, str):
        try:
            return ParamLocation(value)
        except ValueError:
            pass
    return ParamLocation.QUERY


def _servers(document: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for server in document.get("servers") or []:
        if not isinstance(server, dict):
            continue
        url = server.get("url")
        if not isinstance(url, str) or not url:
            continue
        for name, variable in (server.get("variables") or {}).items():
            default = variable.get("default") if isinstance(variable, dict) else None
            if default is not None:
                url = url.replace("{" + str(name) + "}", str(default))
        out.append(url.rstrip("/"))
    return out


def _normalize_security(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {}
            for scheme, scopes in item.items():
                entry[str(scheme)] = [str(s) for s in scopes] if isinstance(scopes, list) else []
            out.append(entry)
    return out


def _media_types(
    content: Any, resolver: RefResolver, document: dict[str, Any]
) -> list[MediaType]:
    media_list: list[MediaType] = []
    if not isinstance(content, dict):
        return media_list
    for media_type, media in content.items():
        if not isinstance(media, dict):
            continue
        raw_schema = media.get("schema")
        schema = normalize_schema(resolver.resolve(raw_schema or {}))
        media_list.append(MediaType(content_type=str(media_type), json_schema=schema))
    return media_list


def _schema_ref_name(raw_schema: Any) -> str | None:
    if isinstance(raw_schema, dict) and isinstance(raw_schema.get("$ref"), str):
        return ref_name(raw_schema["$ref"])
    return None


def _param_from_oas3(raw: dict[str, Any], resolver: RefResolver) -> ParamSpec | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    location = _to_location(raw.get("in"))

    raw_schema = raw.get("schema")
    if raw_schema is None and isinstance(raw.get("content"), dict) and raw["content"]:
        first = next(iter(raw["content"].values()))
        raw_schema = first.get("schema") if isinstance(first, dict) else None

    schema = normalize_schema(resolver.resolve(raw_schema or {}))
    required = bool(raw.get("required")) or location is ParamLocation.PATH
    return ParamSpec(
        name=name,
        location=location,
        required=required,
        json_schema=schema,
        description=raw.get("description") or schema.get("description"),
        example=raw["example"] if "example" in raw else schema.get("example"),
    )


def _merge_params(
    path_level: list[Any],
    op_level: list[Any],
    document: dict[str, Any],
    resolver: RefResolver,
    builder: Any,
) -> list[ParamSpec]:
    """合并 path 级与 operation 级参数。"""
    merged: dict[tuple[str, str], ParamSpec] = {}
    for raw in list(path_level) + list(op_level):
        node = _deref_top(raw, document)
        if not node:
            continue
        spec = builder(node, resolver)
        if spec is None:
            continue
        merged[(spec.name, spec.location.value)] = spec
    return list(merged.values())


def _build_openapi3(document: dict[str, Any], resolver: RefResolver) -> ApiSpec:
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise MalformedSpecError("OpenAPI 文档缺少合法的 'paths' 对象")

    servers = _servers(document)
    global_security = _normalize_security(document.get("security"))
    endpoints: list[EndpointSpec] = []

    for raw_path, raw_item in paths.items():
        if not isinstance(raw_item, dict):
            continue
        item = _deref_top(raw_item, document)
        path_level = item.get("parameters") if isinstance(item.get("parameters"), list) else []

        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue

            params = _merge_params(
                path_level,
                operation.get("parameters") if isinstance(operation.get("parameters"), list) else [],
                document,
                resolver,
                _param_from_oas3,
            )

            request_body: RequestBodySpec | None = None
            raw_body = operation.get("requestBody")
            if isinstance(raw_body, dict):
                body_node = _deref_top(raw_body, document)
                content = _media_types(body_node.get("content"), resolver, document)
                if content:
                    original_schema = None
                    for media in (body_node.get("content") or {}).values():
                        if isinstance(media, dict):
                            original_schema = media.get("schema")
                            break
                    request_body = RequestBodySpec(
                        required=bool(body_node.get("required")),
                        description=body_node.get("description"),
                        ref_name=_schema_ref_name(original_schema),
                        content=content,
                    )

            responses: list[ResponseSpec] = []
            raw_responses = operation.get("responses")
            if isinstance(raw_responses, dict):
                for status, raw_response in raw_responses.items():
                    if not isinstance(raw_response, dict):
                        continue
                    response = _deref_top(raw_response, document)
                    responses.append(
                        ResponseSpec(
                            status_code=str(status),
                            description=response.get("description"),
                            content=_media_types(response.get("content"), resolver, document),
                        )
                    )

            security = (
                _normalize_security(operation.get("security"))
                if "security" in operation
                else global_security
            )

            endpoints.append(
                EndpointSpec(
                    method=method.upper(),
                    path=str(raw_path),
                    operation_id=operation.get("operationId"),
                    summary=operation.get("summary"),
                    description=operation.get("description"),
                    tags=[str(t) for t in operation.get("tags") or [] if isinstance(t, str)],
                    deprecated=bool(operation.get("deprecated")),
                    params=params,
                    request_body=request_body,
                    responses=responses,
                    security=security,
                    server_urls=servers,
                )
            )

    endpoints.sort(key=lambda ep: (ep.path, METHOD_ORDER.get(ep.method.lower(), 99)))
    return ApiSpec(
        title=str(info.get("title") or ""),
        version=str(info.get("version") or ""),
        description=info.get("description"),
        spec_version=str(document.get("openapi") or ""),
        spec_format="openapi3",
        servers=servers,
        endpoints=endpoints,
        schema_names=sorted((document.get("components") or {}).get("schemas", {}) or {}),
    )


def _param_from_swagger2(raw: dict[str, Any], resolver: RefResolver) -> ParamSpec | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    location = _to_location(raw.get("in"))
    schema = normalize_schema(resolver.resolve({k: v for k, v in raw.items() if k in SCHEMA_KEYS}))
    required = bool(raw.get("required")) or location is ParamLocation.PATH
    return ParamSpec(
        name=name,
        location=location,
        required=required,
        json_schema=schema,
        description=raw.get("description") or schema.get("description"),
        example=raw["example"] if "example" in raw else schema.get("example"),
    )


def _build_swagger2(document: dict[str, Any], resolver: RefResolver) -> ApiSpec:
    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise MalformedSpecError("Swagger 文档缺少合法的 'paths' 对象")

    host = document.get("host") or ""
    base_path = document.get("basePath") or ""
    schemes = [s for s in document.get("schemes") or [] if isinstance(s, str)] or ["http"]
    if host:
        servers = [f"{scheme}://{host}{base_path}" for scheme in schemes]
    else:
        servers = [base_path] if base_path else []

    global_consumes = [c for c in document.get("consumes") or [] if isinstance(c, str)]
    global_produces = [c for c in document.get("produces") or [] if isinstance(c, str)]
    global_security = _normalize_security(document.get("security"))
    endpoints: list[EndpointSpec] = []

    for raw_path, raw_item in paths.items():
        if not isinstance(raw_item, dict):
            continue
        item = _deref_top(raw_item, document)
        path_level = item.get("parameters") if isinstance(item.get("parameters"), list) else []

        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue

            consumes = [c for c in operation.get("consumes") or global_consumes or ["application/json"]]
            produces = [p for p in operation.get("produces") or global_produces or ["application/json"]]

            op_level = operation.get("parameters") if isinstance(operation.get("parameters"), list) else []
            merged_raw: dict[tuple[str, str], dict[str, Any]] = {}
            for raw_param in list(path_level) + list(op_level):
                node = _deref_top(raw_param, document)
                if not node:
                    continue
                key = (str(node.get("name")), str(node.get("in")))
                merged_raw[key] = node

            params: list[ParamSpec] = []
            request_body: RequestBodySpec | None = None
            for node in merged_raw.values():
                if node.get("in") == "body":
                    raw_schema = node.get("schema")
                    schema = normalize_schema(resolver.resolve(raw_schema or {}))
                    request_body = RequestBodySpec(
                        required=bool(node.get("required")),
                        description=node.get("description"),
                        ref_name=_schema_ref_name(raw_schema),
                        content=[MediaType(content_type=ct, json_schema=schema) for ct in consumes],
                    )
                    continue
                param = _param_from_swagger2(node, resolver)
                if param is not None:
                    params.append(param)

            responses: list[ResponseSpec] = []
            raw_responses = operation.get("responses")
            if isinstance(raw_responses, dict):
                for status, raw_response in raw_responses.items():
                    response = _deref_top(raw_response, document)
                    if not response:
                        continue
                    raw_schema = response.get("schema")
                    content: list[MediaType] = []
                    if isinstance(raw_schema, dict):
                        schema = normalize_schema(resolver.resolve(raw_schema))
                        content = [MediaType(content_type=ct, json_schema=schema) for ct in produces]
                    responses.append(
                        ResponseSpec(
                            status_code=str(status),
                            description=response.get("description"),
                            content=content,
                        )
                    )

            security = (
                _normalize_security(operation.get("security"))
                if "security" in operation
                else global_security
            )

            endpoints.append(
                EndpointSpec(
                    method=method.upper(),
                    path=str(raw_path),
                    operation_id=operation.get("operationId"),
                    summary=operation.get("summary"),
                    description=operation.get("description"),
                    tags=[str(t) for t in operation.get("tags") or [] if isinstance(t, str)],
                    deprecated=bool(operation.get("deprecated")),
                    params=params,
                    request_body=request_body,
                    responses=responses,
                    security=security,
                    server_urls=servers,
                )
            )

    endpoints.sort(key=lambda ep: (ep.path, METHOD_ORDER.get(ep.method.lower(), 99)))
    return ApiSpec(
        title=str(info.get("title") or ""),
        version=str(info.get("version") or ""),
        description=info.get("description"),
        spec_version=str(document.get("swagger") or ""),
        spec_format="swagger2",
        servers=servers,
        base_path=base_path or None,
        endpoints=endpoints,
        schema_names=sorted((document.get("definitions") or {}) or {}),
    )


def build_api_spec(
    document: dict[str, Any], *, max_ref_depth: int = 48, ref_cache: bool = True
) -> ApiSpec:
    """把一份已加载的文档字典转成 :class:`ApiSpec`。"""
    if not isinstance(document, dict):
        raise MalformedSpecError(f"文档根节点必须是对象, 实际是 {type(document).__name__}")

    resolver_kwargs = {"max_depth": max_ref_depth, "disable_cache": not ref_cache}

    if "openapi" in document:
        version = str(document.get("openapi") or "")
        if not version.startswith("3."):
            raise MalformedSpecError(f"暂不支持的 OpenAPI 版本: {version!r}")
        return _build_openapi3(document, RefResolver(document, **resolver_kwargs))

    if str(document.get("swagger") or "") == "2.0":
        return _build_swagger2(document, RefResolver(document, **resolver_kwargs))

    raise MalformedSpecError("无法识别文档版本: 顶层既没有 'openapi' 也不是 'swagger: 2.0'")
