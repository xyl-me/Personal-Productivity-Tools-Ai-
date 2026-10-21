"""解析层的数据模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .normalize import collect_property_paths


class ParamLocation(str, Enum):
    """参数位置。``FORM`` 对应 Swagger 2.0 的 ``formData``。"""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"
    BODY = "body"
    FORM = "formData"


class ParamSpec(BaseModel):
    """单个参数 (含 path/query/header/cookie, 以及 Swagger 2.0 的 formData)。"""

    model_config = ConfigDict(extra="ignore")

    name: str
    location: ParamLocation
    required: bool = False
    json_schema: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    example: Any = None

    @property
    def type(self) -> str | None:
        value = self.json_schema.get("type")
        return value if isinstance(value, str) else None

    @property
    def format(self) -> str | None:
        return self.json_schema.get("format")

    @property
    def enum(self) -> list[Any]:
        value = self.json_schema.get("enum")
        return list(value) if isinstance(value, list) else []

    @property
    def nullable(self) -> bool:
        return bool(self.json_schema.get("nullable"))

    @property
    def minimum(self) -> float | None:
        value = self.json_schema.get("minimum")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def maximum(self) -> float | None:
        value = self.json_schema.get("maximum")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def min_length(self) -> int | None:
        value = self.json_schema.get("minLength")
        return value if isinstance(value, int) else None

    @property
    def max_length(self) -> int | None:
        value = self.json_schema.get("maxLength")
        return value if isinstance(value, int) else None

    @property
    def pattern(self) -> str | None:
        return self.json_schema.get("pattern")

    @property
    def is_array(self) -> bool:
        return self.type == "array"

    @property
    def item_type(self) -> str | None:
        items = self.json_schema.get("items")
        if isinstance(items, dict):
            value = items.get("type")
            return value if isinstance(value, str) else None
        return None

    def signature(self) -> str:
        """给日志/UI 用的单行描述, 例如 ``order_id (path, 必填, string)``。"""
        flags = [self.location.value]
        if self.required:
            flags.append("必填")
        kind = self.type or "any"
        if self.is_array and self.item_type:
            kind = f"array<{self.item_type}>"
        return f"{self.name} ({', '.join(flags)}, {kind})"


class MediaType(BaseModel):
    """请求体/响应体中的一种 MIME 类型。"""

    model_config = ConfigDict(extra="ignore")

    content_type: str
    json_schema: dict[str, Any] = Field(default_factory=dict)


class RequestBodySpec(BaseModel):
    """请求体。``ref_name`` 记录它最初是哪个 components.schemas 条目, 便于做字段白名单。"""

    model_config = ConfigDict(extra="ignore")

    required: bool = False
    description: str | None = None
    ref_name: str | None = None
    content: list[MediaType] = Field(default_factory=list)

    @property
    def primary(self) -> MediaType | None:
        """优先返回 JSON, 否则返回第一个。"""
        for media in self.content:
            if "json" in media.content_type:
                return media
        return self.content[0] if self.content else None

    @property
    def json_schema(self) -> dict[str, Any]:
        media = self.primary
        return media.json_schema if media else {}


class ResponseSpec(BaseModel):
    """响应定义。``status_code`` 保留原始字符串, 因为文档里可能是 ``default``。"""

    model_config = ConfigDict(extra="ignore")

    status_code: str
    description: str | None = None
    content: list[MediaType] = Field(default_factory=list)

    @property
    def primary(self) -> MediaType | None:
        for media in self.content:
            if "json" in media.content_type:
                return media
        return self.content[0] if self.content else None

    @property
    def json_schema(self) -> dict[str, Any]:
        media = self.primary
        return media.json_schema if media else {}


class EndpointSpec(BaseModel):
    """一个接口 (HTTP method + path)。"""

    model_config = ConfigDict(extra="ignore")

    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    deprecated: bool = False
    params: list[ParamSpec] = Field(default_factory=list)
    request_body: RequestBodySpec | None = None
    responses: list[ResponseSpec] = Field(default_factory=list)
    security: list[dict[str, Any]] = Field(default_factory=list)
    server_urls: list[str] = Field(default_factory=list)

    @property
    def uid(self) -> str:
        """全平台唯一的接口标识, 用作 SQLite 主键 / 用例归属。"""
        return f"{self.method} {self.path}"

    @property
    def summary_or_default(self) -> str:
        return self.summary or self.description or self.operation_id or self.uid

    def param_names(self) -> set[str]:
        """文档里声明的非 body 参数名集合。"""
        return {p.name for p in self.params}

    def param(self, name: str, location: ParamLocation | None = None) -> ParamSpec | None:
        for p in self.params:
            if p.name == name and (location is None or p.location == location):
                return p
        return None

    def body_property_paths(self) -> set[str]:
        """请求体 JSON 中所有合法的字段路径, 例如 ``address.city`` / ``items[].sku``。"""
        return collect_property_paths(self.request_body.json_schema if self.request_body else {})

    def all_param_paths(self) -> set[str]:
        """非 body 参数名 + body 字段路径的并集。"""
        return self.param_names() | self.body_property_paths()


class ApiSpec(BaseModel):
    """整份文档的解析结果。"""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    version: str = ""
    spec_version: str = ""
    spec_format: str = ""
    description: str | None = None
    servers: list[str] = Field(default_factory=list)
    base_path: str | None = None
    endpoints: list[EndpointSpec] = Field(default_factory=list)
    schema_names: list[str] = Field(default_factory=list)

    def endpoint_index(self) -> dict[str, EndpointSpec]:
        return {ep.uid: ep for ep in self.endpoints}

    def find(self, method: str, path: str) -> EndpointSpec | None:
        for ep in self.endpoints:
            if ep.method == method.upper() and ep.path == path:
                return ep
        return None

    def by_tag(self, tag: str) -> list[EndpointSpec]:
        return [ep for ep in self.endpoints if tag in ep.tags]

    @property
    def tags(self) -> list[str]:
        seen: list[str] = []
        for ep in self.endpoints:
            for tag in ep.tags:
                if tag not in seen:
                    seen.append(tag)
        return seen
