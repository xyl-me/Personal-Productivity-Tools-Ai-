"""合成大规模 OpenAPI 文档。"""

from __future__ import annotations

from typing import Any


def _model(index: int, model_count: int) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "id": {"type": "integer"},
        "name": {"type": "string", "maxLength": 64, "minLength": 1},
        "status": {"type": "string", "enum": ["CREATED", "PAID", "CANCELLED"]},
        "amount": {"type": "number", "minimum": 0, "maximum": 100000},
        "created_at": {"type": "string", "format": "date-time"},
        "tags": {"type": "array", "items": {"type": "string"}},
    }
    if index > 0:
        properties["parent"] = {"$ref": f"#/components/schemas/Model{index - 1}"}
    if index % 7 == 0:
        properties["related"] = {
            "type": "array",
            "items": {"$ref": f"#/components/schemas/Model{(index + 1) % model_count}"},
        }
    return {"type": "object", "required": ["id", "name"], "properties": properties}


def build_document(*, endpoints: int = 200, models: int = 40) -> dict[str, Any]:
    """生成一份含 ``endpoints`` 个操作的文档。"""
    if endpoints <= 0 or models <= 0:
        raise ValueError("endpoints 与 models 必须为正数")

    schemas: dict[str, Any] = {
        f"Model{index}": _model(index, models) for index in range(models)
    }
    schemas["PageMeta"] = {
        "type": "object",
        "required": ["page", "page_size", "total"],
        "properties": {
            "page": {"type": "integer", "minimum": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100},
            "total": {"type": "integer"},
        },
    }
    schemas["AuditedModel0"] = {
        "allOf": [
            {"$ref": "#/components/schemas/Model0"},
            {
                "type": "object",
                "properties": {
                    "created_by": {"type": "string"},
                    "updated_at": {"type": "string", "format": "date-time"},
                },
            },
        ]
    }

    paths: dict[str, Any] = {}
    operations = 0
    resource = 0
    while operations < endpoints:
        model = f"Model{resource % models}"
        envelope = {
            "type": "object",
            "allOf": [
                {"$ref": "#/components/schemas/PageMeta"},
                {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"$ref": f"#/components/schemas/{model}"}},
                    },
                },
            ],
        }

        list_path = f"/api/v1/resource{resource}"
        paths[list_path] = {
            "get": {
                "operationId": f"listResource{resource}",
                "tags": [f"resource{resource % 12}"],
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                    {
                        "name": "page_size",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    {"name": "keyword", "in": "query", "schema": {"type": "string"}},
                    {
                        "name": "X-Tenant-Id",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "maxLength": 32},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": envelope}},
                    }
                },
            },
            "post": {
                "operationId": f"createResource{resource}",
                "tags": [f"resource{resource % 12}"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}}
                    },
                },
                "responses": {
                    "201": {
                        "description": "Created",
                        "content": {
                            "application/json": {"schema": {"$ref": f"#/components/schemas/{model}"}}
                        },
                    }
                },
            },
        }
        operations += 2

        if operations < endpoints:
            detail_path = f"{list_path}/{{item_id}}"
            paths[detail_path] = {
                "get": {
                    "operationId": f"getResource{resource}",
                    "tags": [f"resource{resource % 12}"],
                    "parameters": [
                        {"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer"}}
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"#/components/schemas/{model}"}
                                }
                            },
                        }
                    },
                }
            }
            operations += 1
        resource += 1

    return {
        "openapi": "3.0.3",
        "info": {"title": f"Synthetic API ({operations} ops)", "version": "1.0.0"},
        "servers": [{"url": "http://127.0.0.1:8000/api/v1"}],
        "x-generated-operations": operations,
        "paths": paths,
        "components": {"schemas": schemas},
    }
