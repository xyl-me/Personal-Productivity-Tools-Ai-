"""``$ref`` 解析。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import MalformedSpecError, UnresolvedRefError

CIRCULAR_MARKER = "x-circular-ref"
DEPTH_MARKER = "x-depth-truncated"


def unescape_token(token: str) -> str:
    """RFC 6901: ``~1`` -> ``/``, ``~0`` -> ``~`` (顺序不能反)。"""
    return token.replace("~1", "/").replace("~0", "~")


def parse_pointer(pointer: str) -> list[str]:
    """把 ``#/components/schemas/Pet`` 拆成 ``["components", "schemas", "Pet"]``。"""
    if pointer in ("", "#"):
        return []
    if not pointer.startswith("#"):
        raise UnresolvedRefError(pointer, "不支持外部文件引用 (只支持文档内 #/... 引用)")
    body = pointer[1:]
    if not body.startswith("/"):
        raise UnresolvedRefError(pointer, "引用格式不合法, 应以 '#/' 开头")
    return [unescape_token(token) for token in body[1:].split("/")]


def resolve_pointer(document: Any, pointer: str) -> Any:
    """按 JSON Pointer 从文档中取值, 取不到时抛 :class:`UnresolvedRefError`。"""
    current = document
    for token in parse_pointer(pointer):
        if isinstance(current, dict):
            if token not in current:
                raise UnresolvedRefError(pointer, f"路径中缺少键 {token!r}")
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise UnresolvedRefError(pointer, f"数组下标 {token!r} 越界") from exc
        else:
            raise UnresolvedRefError(pointer, f"无法在 {type(current).__name__} 上继续取值")
    return current


def ref_name(ref: str) -> str | None:
    """取 ref 的最后一段, 例如 ``#/components/schemas/Order`` -> ``Order``。"""
    try:
        tokens = parse_pointer(ref)
    except UnresolvedRefError:
        return None
    return tokens[-1] if tokens else None


@dataclass
class RefResolver:
    """把一个已 ``json.loads`` 的文档里所有 ``$ref`` 就地展开。"""

    document: dict[str, Any]
    max_depth: int = 48
    _resolved_refs: set[str] = field(default_factory=set)
    _cache: dict[str, Any] = field(default_factory=dict)
    disable_cache: bool = False
    cache_hits: int = 0

    def resolve(self, node: Any) -> Any:
        """返回展开后的**新对象**, 不改动 ``document``。"""
        return self._resolve(node, stack=(), depth=0)

    def _resolve(self, node: Any, stack: tuple[str, ...], depth: int) -> Any:
        if depth > self.max_depth:
            return {DEPTH_MARKER: True, "description": f"超过最大展开深度 {self.max_depth}"}

        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in stack:
                    return {
                        CIRCULAR_MARKER: ref,
                        "type": "object",
                        "description": f"递归引用 {ref} (已在此处截断)",
                    }

                siblings = {k: v for k, v in node.items() if k != "$ref"}
                cacheable = not stack and not siblings and not self.disable_cache
                if cacheable and ref in self._cache:
                    self.cache_hits += 1
                    return self._cache[ref]

                target = resolve_pointer(self.document, ref)
                self._resolved_refs.add(ref)
                resolved = self._resolve(target, stack + (ref,), depth + 1)

                if not siblings:
                    if cacheable:
                        self._cache[ref] = resolved
                    return resolved
                resolved_siblings = {k: self._resolve(v, stack, depth + 1) for k, v in siblings.items()}
                if isinstance(resolved, dict):
                    merged = dict(resolved)
                    merged.update(resolved_siblings)
                    return merged
                return {"allOf": [resolved, resolved_siblings]}

            return {key: self._resolve(value, stack, depth + 1) for key, value in node.items()}

        if isinstance(node, list):
            return [self._resolve(item, stack, depth + 1) for item in node]

        return node

    def assert_resolvable(self, pointer: str) -> None:
        """显式检查某个引用是否存在 (用于解析失败时的快速定位)。"""
        try:
            resolve_pointer(self.document, pointer)
        except UnresolvedRefError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            raise MalformedSpecError(f"引用 {pointer!r} 解析异常: {exc}") from exc
