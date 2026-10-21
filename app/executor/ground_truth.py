"""靶场的缺陷指纹表 (Ground Truth 的可执行版本)。"""

from __future__ import annotations

from typing import Any

from .models import CaseResult, FailureKind
from .triage import DefectSignature

_NO_BODY = FailureKind.SERVER_ERROR
_ACCEPTED = FailureKind.UNEXPECTED_SUCCESS


def _number(container: Any, key: str) -> float | None:
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def _negative_amount(case: Any, result: CaseResult) -> bool:
    amount = _number(case.body, "amount")
    return amount is not None and amount < 0


def _non_positive_quantity(case: Any, result: CaseResult) -> bool:
    quantity = _number(case.body, "quantity")
    return quantity is not None and quantity <= 0


def _invalid_pagination(case: Any, result: CaseResult) -> bool:
    page = _number(case.query, "page")
    page_size = _number(case.query, "page_size")
    return (page is not None and page < 1) or (page_size is not None and page_size < 1)


GROUND_TRUTH: tuple[DefectSignature, ...] = (
    DefectSignature(
        id="D1",
        title="备注超长导致 500",
        category="文档与实现不一致",
        endpoint="POST /api/v1/orders",
        failure_kinds=frozenset({_NO_BODY}),
    ),
    DefectSignature(
        id="D2",
        title="负金额订单可创建",
        category="业务规则缺失",
        endpoint="POST /api/v1/orders",
        failure_kinds=frozenset({_ACCEPTED}),
        extra=_negative_amount,
    ),
    DefectSignature(
        id="D3",
        title="数量为 0/负数可创建",
        category="业务规则缺失",
        endpoint="POST /api/v1/orders",
        failure_kinds=frozenset({_ACCEPTED}),
        extra=_non_positive_quantity,
    ),
    DefectSignature(
        id="D4",
        title="搜索接口 SQL 注入",
        category="安全",
        endpoint="GET /api/v1/orders/search",
        failure_kinds=frozenset(
            {FailureKind.CONTENT_MISMATCH, FailureKind.SERVER_ERROR, FailureKind.UNEXPECTED_SUCCESS}
        ),
    ),
    DefectSignature(
        id="D5",
        title="分页参数越界未校验",
        category="边界值缺失",
        endpoint="GET /api/v1/orders",
        failure_kinds=frozenset({_ACCEPTED, FailureKind.CONTENT_MISMATCH}),
        extra=_invalid_pagination,
    ),
    DefectSignature(
        id="D6",
        title="路径参数非数字导致 500",
        category="类型错误未容错",
        endpoint="GET /api/v1/orders/{order_id}",
        failure_kinds=frozenset({_NO_BODY}),
    ),
    DefectSignature(
        id="D7",
        title="非法状态值被写入",
        category="枚举白名单缺失",
        endpoint="PUT /api/v1/orders/{order_id}/status",
        failure_kinds=frozenset({_ACCEPTED}),
    ),
    DefectSignature(
        id="D8",
        title="管理接口越权导出",
        category="鉴权缺失",
        endpoint="GET /api/v1/admin/orders/export",
        failure_kinds=frozenset({_ACCEPTED}),
    ),
)
