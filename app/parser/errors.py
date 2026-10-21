"""解析层异常类型。"""

from __future__ import annotations


class SpecError(Exception):
    """解析层所有错误的基类。"""


class MalformedSpecError(SpecError):
    """文档结构不合法 (不是 dict、缺少 paths、版本无法识别等)。"""


class UnresolvedRefError(SpecError):
    """``$ref`` 指向的节点不存在, 或指向了外部文件 (当前不支持)。"""

    def __init__(self, ref: str, reason: str = "") -> None:
        self.ref = ref
        self.reason = reason
        message = f"无法解析引用 {ref!r}"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)
