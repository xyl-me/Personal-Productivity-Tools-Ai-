"""Swagger / OpenAPI 文档解析层。"""

from .api import load_spec, parse_spec, parse_spec_dict, parse_spec_file
from .errors import MalformedSpecError, SpecError, UnresolvedRefError
from .models import (
    ApiSpec,
    EndpointSpec,
    MediaType,
    ParamLocation,
    ParamSpec,
    RequestBodySpec,
    ResponseSpec,
)
from .normalize import collect_property_paths, merge_all_of, normalize_schema
from .refs import CIRCULAR_MARKER, RefResolver, resolve_pointer
from .render import render_api_catalog, render_endpoint_markdown, schema_to_compact_json

__all__ = [
    "ApiSpec",
    "CIRCULAR_MARKER",
    "EndpointSpec",
    "MalformedSpecError",
    "MediaType",
    "ParamLocation",
    "ParamSpec",
    "RefResolver",
    "RequestBodySpec",
    "ResponseSpec",
    "SpecError",
    "UnresolvedRefError",
    "collect_property_paths",
    "load_spec",
    "merge_all_of",
    "normalize_schema",
    "parse_spec",
    "parse_spec_dict",
    "parse_spec_file",
    "render_api_catalog",
    "render_endpoint_markdown",
    "resolve_pointer",
    "schema_to_compact_json",
]
