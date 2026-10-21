"""接口提取测试: OpenAPI 3 与 Swagger 2.0 都要能出一致的 ApiSpec。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.parser import (
    ParamLocation,
    parse_spec,
    parse_spec_dict,
    parse_spec_file,
    render_api_catalog,
    render_endpoint_markdown,
)
from app.parser.errors import MalformedSpecError

FIXTURE = Path(__file__).parent / "fixtures" / "mini_openapi.json"


def test_metadata_and_servers_with_variables() -> None:
    spec = parse_spec_file(FIXTURE)
    assert spec.title == "Mini Order API"
    assert spec.version == "1.2.0"
    assert spec.spec_format == "openapi3"
    assert spec.spec_version == "3.0.3"
    assert spec.servers == ["http://127.0.0.1:8000/api/v1"]


def test_all_endpoints_extracted() -> None:
    spec = parse_spec_file(FIXTURE)
    assert [ep.uid for ep in spec.endpoints] == [
        "GET /nodes/{node_id}",
        "GET /orders",
        "POST /orders",
        "GET /orders/{order_id}",
    ]


def test_path_level_parameters_are_inherited() -> None:
    spec = parse_spec_file(FIXTURE)
    for method in ("GET", "POST"):
        endpoint = spec.find(method, "/orders")
        assert endpoint is not None
        tenant = endpoint.param("X-Tenant-Id", ParamLocation.HEADER)
        assert tenant is not None
        assert tenant.required is True


def test_operation_parameter_overrides_path_parameter() -> None:
    spec = parse_spec_dict(
        {
            "openapi": "3.0.0",
            "info": {"title": "t", "version": "1"},
            "paths": {
                "/x": {
                    "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                    "get": {
                        "parameters": [
                            {"name": "q", "in": "query", "required": True,
                             "schema": {"type": "integer", "minimum": 2}}
                        ],
                        "responses": {"200": {"description": "ok"}},
                    },
                }
            },
        }
    )
    endpoint = spec.endpoints[0]
    assert len([p for p in endpoint.params if p.name == "q"]) == 1
    assert endpoint.params[0].required is True
    assert endpoint.params[0].type == "integer"
    assert endpoint.params[0].minimum == 2


def test_path_parameters_are_always_required() -> None:
    spec = parse_spec_file(FIXTURE)
    endpoint = spec.find("GET", "/orders/{order_id}")
    assert endpoint is not None
    order_id = endpoint.param("order_id")
    assert order_id is not None
    assert order_id.required is True


def test_query_parameter_constraints_are_kept() -> None:
    spec = parse_spec_file(FIXTURE)
    endpoint = spec.find("GET", "/orders")
    assert endpoint is not None
    page_size = endpoint.param("page_size")
    assert page_size is not None
    assert (page_size.minimum, page_size.maximum) == (1, 100)
    status = endpoint.param("status")
    assert status is not None
    assert status.enum == ["CREATED", "PAID"]


def test_request_body_ref_name_and_allof_flattened() -> None:
    spec = parse_spec_file(FIXTURE)
    endpoint = spec.find("POST", "/orders")
    assert endpoint is not None
    body = endpoint.request_body
    assert body is not None
    assert body.required is True
    assert body.ref_name == "CreateOrderRequest"
    assert set(body.json_schema["properties"]) == {
        "item_id",
        "quantity",
        "amount",
        "remark",
        "coupon",
        "channel",
    }
    assert body.json_schema["required"] == ["item_id", "quantity"]


def test_nullable_field_collapsed_to_single_type() -> None:
    spec = parse_spec_file(FIXTURE)
    endpoint = spec.find("POST", "/orders")
    assert endpoint is not None and endpoint.request_body is not None
    props = endpoint.request_body.json_schema["properties"]
    assert props["channel"]["type"] == "string"
    assert props["channel"]["nullable"] is True
    assert props["coupon"]["nullable"] is True


def test_body_property_paths_used_for_param_whitelist() -> None:
    spec = parse_spec_file(FIXTURE)
    endpoint = spec.find("POST", "/orders")
    assert endpoint is not None
    assert endpoint.body_property_paths() == {
        "item_id",
        "quantity",
        "amount",
        "remark",
        "coupon",
        "channel",
    }
    assert endpoint.all_param_paths() >= endpoint.param_names()


def test_responses_and_security() -> None:
    spec = parse_spec_file(FIXTURE)
    post = spec.find("POST", "/orders")
    assert post is not None
    assert [r.status_code for r in post.responses] == ["201", "400"]
    assert post.security == [{"ApiKeyAuth": []}]

    get = spec.find("GET", "/orders")
    assert get is not None
    assert get.security == []


def test_circular_response_schema_does_not_hang() -> None:
    spec = parse_spec_file(FIXTURE)
    endpoint = spec.find("GET", "/nodes/{node_id}")
    assert endpoint is not None
    schema = endpoint.responses[0].json_schema
    assert schema["properties"]["parent"]["x-circular-ref"] == "#/components/schemas/Node"


def test_render_endpoint_markdown_is_prompt_ready() -> None:
    spec = parse_spec_file(FIXTURE)
    endpoint = spec.find("POST", "/orders")
    assert endpoint is not None
    text = render_endpoint_markdown(endpoint)
    assert "## POST /orders" in text
    assert "`X-Tenant-Id`" in text
    assert "合法字段" in text
    assert "<truncated>" not in text
    assert text.count("| `") == len(endpoint.params)


def test_render_api_catalog_lists_every_endpoint() -> None:
    spec = parse_spec_file(FIXTURE)
    catalog = render_api_catalog(spec)
    assert "共 4 个接口" in catalog
    for endpoint in spec.endpoints:
        assert endpoint.path in catalog


SWAGGER2 = {
    "swagger": "2.0",
    "info": {"title": "Legacy User API", "version": "0.9"},
    "host": "api.example.com",
    "basePath": "/v1",
    "schemes": ["https"],
    "consumes": ["application/json"],
    "produces": ["application/json"],
    "security": [{"api_key": []}],
    "securityDefinitions": {"api_key": {"type": "apiKey", "name": "X-API-KEY", "in": "header"}},
    "paths": {
        "/users/{id}": {
            "get": {
                "summary": "查询用户",
                "parameters": [
                    {"name": "id", "in": "path", "type": "integer", "required": True},
                    {"name": "fields", "in": "query", "type": "string"},
                ],
                "responses": {
                    "200": {"description": "OK", "schema": {"$ref": "#/definitions/User"}}
                },
            },
            "put": {
                "summary": "更新用户",
                "parameters": [
                    {"name": "id", "in": "path", "type": "integer", "required": True},
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "schema": {"$ref": "#/definitions/User"},
                    },
                ],
                "responses": {"204": {"description": "No Content"}},
            },
        },
        "/upload": {
            "post": {
                "summary": "上传文件",
                "consumes": ["multipart/form-data"],
                "parameters": [
                    {"name": "file", "in": "formData", "type": "file", "required": True},
                    {"name": "note", "in": "formData", "type": "string"},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
    },
    "definitions": {
        "User": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }
    },
}


def test_swagger2_servers_from_host_and_basepath() -> None:
    spec = parse_spec_dict(SWAGGER2)
    assert spec.spec_format == "swagger2"
    assert spec.servers == ["https://api.example.com/v1"]


def test_swagger2_body_parameter_becomes_request_body() -> None:
    spec = parse_spec_dict(SWAGGER2)
    endpoint = spec.find("PUT", "/users/{id}")
    assert endpoint is not None
    body = endpoint.request_body
    assert body is not None
    assert body.required is True
    assert body.ref_name == "User"
    assert body.primary is not None
    assert body.primary.content_type == "application/json"
    assert body.json_schema["properties"]["tags"]["items"]["type"] == "string"
    assert "body" not in endpoint.param_names()


def test_swagger2_form_data_parameters() -> None:
    spec = parse_spec_dict(SWAGGER2)
    endpoint = spec.find("POST", "/upload")
    assert endpoint is not None
    names = {p.name: p for p in endpoint.params}
    assert names["file"].location is ParamLocation.FORM
    assert names["file"].required is True
    assert names["note"].location is ParamLocation.FORM
    assert endpoint.request_body is None


def test_swagger2_inline_param_constraints() -> None:
    spec = parse_spec_dict(SWAGGER2)
    endpoint = spec.find("GET", "/users/{id}")
    assert endpoint is not None
    user_id = endpoint.param("id")
    assert user_id is not None
    assert user_id.type == "integer"
    assert user_id.required is True


def test_swagger2_global_security_inherited() -> None:
    spec = parse_spec_dict(SWAGGER2)
    assert spec.find("GET", "/users/{id}").security == [{"api_key": []}]  # type: ignore[union-attr]


def test_swagger2_response_schema_uses_produces() -> None:
    spec = parse_spec_dict(SWAGGER2)
    endpoint = spec.find("GET", "/users/{id}")
    assert endpoint is not None
    response = endpoint.responses[0]
    assert response.content[0].content_type == "application/json"
    assert set(response.json_schema["properties"]) == {"id", "name", "tags"}


def test_parse_spec_accepts_json_text() -> None:
    spec = parse_spec(FIXTURE.read_text(encoding="utf-8"))
    assert spec.spec_format == "openapi3"


def test_parse_spec_rejects_unknown_version() -> None:
    with pytest.raises(MalformedSpecError):
        parse_spec_dict({"info": {"title": "x"}, "paths": {}})


def test_parse_spec_rejects_missing_paths() -> None:
    with pytest.raises(MalformedSpecError):
        parse_spec_dict({"openapi": "3.0.0", "info": {"title": "x", "version": "1"}})


def test_parse_spec_file_reports_missing_file(tmp_dir: Path) -> None:
    with pytest.raises(MalformedSpecError):
        parse_spec_file(tmp_dir / "nope.json")


def test_yaml_spec_is_supported(tmp_dir: Path) -> None:
    yaml_file = tmp_dir / "api.yaml"
    yaml_file.write_text(
        """
openapi: 3.0.1
info:
  title: YAML API
  version: '2.0'
paths:
  /ping:
    get:
      summary: ping
      responses:
        '200':
          description: ok
""",
        encoding="utf-8",
    )
    spec = parse_spec_file(yaml_file)
    assert spec.title == "YAML API"
    assert spec.find("GET", "/ping") is not None
