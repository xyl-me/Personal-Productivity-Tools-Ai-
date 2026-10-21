"""把解析结果渲染成大模型友好的文本。"""

from __future__ import annotations

import json
from typing import Any

from .models import EndpointSpec, ParamLocation, ParamSpec
from .normalize import collect_property_paths

_LOCATION_LABEL = {
    ParamLocation.PATH: "path",
    ParamLocation.QUERY: "query",
    ParamLocation.HEADER: "header",
    ParamLocation.COOKIE: "cookie",
    ParamLocation.BODY: "body",
    ParamLocation.FORM: "form",
}

_MAX_PROPS = 64
_MAX_ITEMS = 16


def _cell(value: Any, default: str = "-") -> str:
    """表格单元格转义: 竖线会破坏 markdown 表格, 换行会破坏整行。"""
    if value is None or value == "":
        return default
    text = str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()
    return text or default


def _type_text(schema: dict[str, Any]) -> str:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = "/".join(str(k) for k in kind)
    if not isinstance(kind, str) or not kind:
        if "properties" in schema:
            kind = "object"
        elif "x-circular-ref" in schema:
            kind = "object(recursive)"
        else:
            kind = "any"
    if kind == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            kind = f"array<{_type_text(items)}>"
    return kind


def _constraints_text(schema: dict[str, Any]) -> str:
    bits: list[str] = []
    if schema.get("format"):
        bits.append(f"format={schema['format']}")
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        preview = ", ".join(str(v) for v in enum[:8])
        if len(enum) > 8:
            preview = f"{preview}, ..."
        bits.append(f"enum=[{preview}]")
    for key, label in (
        ("minimum", "min"),
        ("maximum", "max"),
        ("minLength", "minLen"),
        ("maxLength", "maxLen"),
        ("minItems", "minItems"),
        ("maxItems", "maxItems"),
    ):
        if key in schema and not isinstance(schema[key], bool):
            bits.append(f"{label}={schema[key]}")
    if schema.get("pattern"):
        bits.append(f"pattern={schema['pattern']}")
    if schema.get("default") is not None:
        bits.append(f"default={schema['default']}")
    if schema.get("nullable"):
        bits.append("nullable")
    if "x-circular-ref" in schema:
        bits.append("递归结构(已截断)")
    return "; ".join(bits) or "-"


def _truncate(node: Any, max_depth: int, depth: int = 0) -> Any:
    if isinstance(node, dict):
        if depth >= max_depth:
            return "<truncated>"
        out: dict[str, Any] = {}
        for index, (key, value) in enumerate(node.items()):
            if index >= _MAX_PROPS:
                out["_keys_omitted"] = len(node) - _MAX_PROPS
                break
            out[key] = _truncate(value, max_depth, depth + 1)
        return out
    if isinstance(node, list):
        if depth >= max_depth:
            return "<truncated>"
        return [_truncate(item, max_depth, depth + 1) for item in node[:_MAX_ITEMS]]
    return node


def schema_to_compact_json(schema: dict[str, Any], *, max_depth: int = 5) -> str:
    """紧凑 JSON, 控制深度与宽度, 保证 prompt 长度可预期。"""
    return json.dumps(_truncate(schema, max_depth), ensure_ascii=False, separators=(",", ":"))


def _table_row(cells: list[Any]) -> str:
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def render_endpoint_markdown(endpoint: EndpointSpec) -> str:
    """把单个接口渲染成 markdown, 供 Prompt 直接拼接。"""
    lines: list[str] = [f"## {endpoint.method} {endpoint.path}", ""]

    if endpoint.summary:
        lines.append(f"- 摘要: {endpoint.summary}")
    if endpoint.description and endpoint.description != endpoint.summary:
        lines.append(f"- 描述: {_cell(endpoint.description)}")
    if endpoint.tags:
        lines.append(f"- 标签: {', '.join(endpoint.tags)}")
    if endpoint.deprecated:
        lines.append("- 状态: 已废弃 (deprecated)")
    if endpoint.security:
        schemes = sorted({name for entry in endpoint.security for name in entry})
        lines.append(f"- 鉴权: {', '.join(schemes) if schemes else '无'}")
    else:
        lines.append("- 鉴权: 无")

    if endpoint.params:
        lines += [
            "",
            "### 参数",
            "",
            "| 参数名 | 位置 | 必填 | 类型 | 约束 | 说明 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for param in endpoint.params:
            lines.append(
                _table_row(
                    [
                        f"`{param.name}`",
                        _LOCATION_LABEL.get(param.location, param.location.value),
                        "是" if param.required else "否",
                        _type_text(param.json_schema),
                        _constraints_text(param.json_schema),
                        param.description,
                    ]
                )
            )

    if endpoint.request_body:
        body = endpoint.request_body
        lines += [
            "",
            "### 请求体",
            "",
            f"- Content-Type: {', '.join(m.content_type for m in body.content)}",
            f"- 必填: {'是' if body.required else '否'}",
        ]
        if body.ref_name:
            lines.append(f"- 结构名: `{body.ref_name}`")
        paths = sorted(collect_property_paths(body.json_schema))
        if paths:
            lines += ["- 合法字段: " + ", ".join(f"`{p}`" for p in paths)]
        lines += ["", "```json", schema_to_compact_json(body.json_schema), "```"]

    if endpoint.responses:
        lines += ["", "### 响应", "", "| 状态码 | 说明 | 主要字段 |", "| --- | --- | --- |"]
        for response in endpoint.responses:
            fields = sorted(collect_property_paths(response.json_schema))[:10]
            lines.append(
                _table_row(
                    [
                        response.status_code,
                        response.description,
                        ", ".join(fields) if fields else "-",
                    ]
                )
            )

    return "\n".join(lines)


def render_api_catalog(api: Any) -> str:
    """整份文档的接口目录, 用于"先让模型挑接口/分批"的场景。"""
    lines = [f"# {_cell(api.title, 'API')} {api.version}".strip(), ""]
    lines.append(f"共 {len(api.endpoints)} 个接口。")
    lines += ["", "| # | 方法 | 路径 | 摘要 | 标签 |", "| --- | --- | --- | --- | --- |"]
    for index, endpoint in enumerate(api.endpoints, start=1):
        lines.append(
            _table_row(
                [
                    index,
                    endpoint.method,
                    f"`{endpoint.path}`",
                    endpoint.summary_or_default,
                    ", ".join(endpoint.tags),
                ]
            )
        )
    return "\n".join(lines)


def render_param_brief(param: ParamSpec) -> str:
    """单行参数描述, 用于日志与断言失败信息。"""
    return param.signature()
