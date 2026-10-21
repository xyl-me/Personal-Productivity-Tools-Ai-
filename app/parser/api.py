"""解析层对外门面: 加载文件 -> 构建 :class:`ApiSpec`。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .errors import MalformedSpecError
from .extractor import build_api_spec
from .models import ApiSpec

_YAML_SUFFIXES = {".yaml", ".yml"}


def load_spec(path: str | Path) -> dict[str, Any]:
    """加载 JSON / YAML 格式的接口文档, 返回原始字典 (尚未展开 $ref)。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise MalformedSpecError(f"文件不存在: {file_path}")

    text = file_path.read_text(encoding="utf-8-sig")
    if file_path.suffix.lower() in _YAML_SUFFIXES:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise MalformedSpecError(f"YAML 解析失败: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedSpecError(f"JSON 解析失败: {exc}") from exc

    if not isinstance(data, dict):
        raise MalformedSpecError("文档根节点必须是对象")
    return data


def parse_spec_dict(document: dict[str, Any], *, ref_cache: bool = True) -> ApiSpec:
    """解析内存中的文档字典。"""
    return build_api_spec(document, ref_cache=ref_cache)


def parse_spec_file(path: str | Path, *, ref_cache: bool = True) -> ApiSpec:
    """解析接口文档文件 (.json / .yaml / .yml)。"""
    return build_api_spec(load_spec(path), ref_cache=ref_cache)


def parse_spec(
    source: dict[str, Any] | str | Path, *, ref_cache: bool = True
) -> ApiSpec:
    """兼容入口: 传字典、文件路径或 JSON 文本都可以。"""
    if isinstance(source, dict):
        return parse_spec_dict(source, ref_cache=ref_cache)
    if isinstance(source, Path):
        return parse_spec_file(source, ref_cache=ref_cache)
    if isinstance(source, str):
        stripped = source.lstrip()
        if stripped.startswith("{"):
            try:
                document = json.loads(source)
            except json.JSONDecodeError as exc:
                raise MalformedSpecError(f"JSON 解析失败: {exc}") from exc
            if not isinstance(document, dict):
                raise MalformedSpecError("文档根节点必须是对象")
            return parse_spec_dict(document, ref_cache=ref_cache)
        return parse_spec_file(source, ref_cache=ref_cache)
    raise MalformedSpecError(f"不支持的输入类型: {type(source).__name__}")
