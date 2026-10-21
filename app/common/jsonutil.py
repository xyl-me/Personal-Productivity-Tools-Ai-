"""容错 JSON 解析。"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

_FENCE_START = re.compile(r"^```[a-zA-Z]*\s*")


class JsonParseError(RuntimeError):
    """文本无法解析成预期的 JSON 结构。"""


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_START.sub("", stripped)
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def loads_tolerant(text: str) -> Any:
    """解析 JSON; 失败时尝试截取第一个 ``{`` 到最后一个 ``}`` 之间的内容。"""
    if not text or not text.strip():
        raise JsonParseError("模型返回了空内容")

    cleaned = strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise JsonParseError(f"JSON 解析失败: {exc.msg} (第 {exc.lineno} 行)") from exc


def format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:3]:
        location = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{location}: {error.get('msg')}")
    return "; ".join(parts) or str(exc)
