"""靶场: 一个**故意埋了缺陷**的订单服务。"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

ADMIN_TOKEN = "secret-admin-token"

app = FastAPI(
    title="Demo Order Service",
    version="1.0.0",
    description="一个故意埋了缺陷的订单服务, 用作接口测试用例生成与评测的靶场。",
)

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.isolation_level = None
_conn.row_factory = sqlite3.Row
_lock = threading.RLock()

_DDL = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED',
    remark TEXT CHECK (remark IS NULL OR length(remark) <= 20),
    created_at TEXT NOT NULL
);
"""

SEED_ORDERS: list[tuple[str, int, float, str, str | None]] = [
    ("SKU-1001", 2, 199.0, "CREATED", None),
    ("SKU-1002", 1, 59.9, "PAID", "gift wrap"),
    ("SKU-1003", 5, 880.0, "CREATED", None),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _init_db() -> None:
    with _lock:
        _conn.execute(_DDL)
        _conn.executemany(
            "INSERT INTO orders (item_id, quantity, amount, status, remark, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(item, qty, amount, status, remark, _now()) for item, qty, amount, status, remark in SEED_ORDERS],
        )


def reset_demo_data() -> None:
    """把数据恢复到初始状态。测试与演示都依赖它保证可复现。"""
    with _lock:
        _conn.execute("DROP TABLE IF EXISTS orders")
    _init_db()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _fetch(order_id: int) -> dict[str, Any] | None:
    with _lock:
        row = _conn.execute(
            "SELECT id, item_id, quantity, amount, status, remark, created_at"
            " FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


_init_db()


class CreateOrderRequest(BaseModel):
    item_id: str = Field(..., description="商品编号", min_length=1, max_length=32)
    quantity: int = Field(..., description="购买数量")
    amount: float = Field(..., description="订单金额(元)")
    remark: str | None = Field(None, max_length=200, description="订单备注")


class StatusUpdate(BaseModel):
    status: str = Field(..., description="目标状态, 如 CREATED / PAID / CANCELLED")


class OrderOut(BaseModel):
    id: int
    item_id: str
    quantity: int
    amount: float
    status: str
    remark: str | None = None
    created_at: str


@app.post(
    "/api/v1/orders",
    response_model=OrderOut,
    status_code=201,
    summary="创建订单",
    tags=["orders"],
)
def create_order(payload: CreateOrderRequest = Body(...)) -> dict[str, Any]:
    with _lock:
        cursor = _conn.execute(
            "INSERT INTO orders (item_id, quantity, amount, status, remark, created_at)"
            " VALUES (?, ?, ?, 'CREATED', ?, ?)",
            (payload.item_id, payload.quantity, payload.amount, payload.remark, _now()),
        )
        created = _fetch(int(cursor.lastrowid))
    assert created is not None
    return created


@app.get(
    "/api/v1/orders/search",
    response_model=list[OrderOut],
    summary="按商品编号搜索订单",
    tags=["orders"],
)
def search_orders(q: str = Query("", description="商品编号关键字, 支持模糊匹配")) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, item_id, quantity, amount, status, remark, created_at"
        f" FROM orders WHERE item_id LIKE '%{q}%'"
    )
    with _lock:
        rows = _conn.execute(sql).fetchall()
    return [_row_to_dict(row) for row in rows]


@app.get(
    "/api/v1/orders",
    response_model=list[OrderOut],
    summary="分页查询订单",
    tags=["orders"],
)
def list_orders(
    page: int = Query(1, description="页码, 从 1 开始"),
    page_size: int = Query(10, description="每页条数"),
) -> list[dict[str, Any]]:
    offset = (page - 1) * page_size
    with _lock:
        rows = _conn.execute(
            "SELECT id, item_id, quantity, amount, status, remark, created_at"
            " FROM orders ORDER BY id LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


@app.get(
    "/api/v1/orders/{order_id}",
    response_model=OrderOut,
    summary="按 ID 查询订单",
    tags=["orders"],
)
def get_order(order_id: str = Path(..., description="订单 ID(数字字符串)")) -> dict[str, Any]:
    order = _fetch(int(order_id))
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@app.put(
    "/api/v1/orders/{order_id}/status",
    summary="更新订单状态",
    tags=["orders"],
)
def update_status(order_id: int, body: StatusUpdate = Body(...)) -> dict[str, Any]:
    with _lock:
        cursor = _conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (body.status, order_id)
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="订单不存在")
    return {"id": order_id, "status": body.status}


@app.delete(
    "/api/v1/orders/{order_id}",
    summary="删除订单(需要管理员令牌)",
    tags=["orders"],
)
def delete_order(
    order_id: int,
    x_admin_token: str | None = Header(None, alias="X-Admin-Token", description="管理员令牌"),
) -> dict[str, Any]:
    """对照组: 这个接口的鉴权与 404 处理是**正确**的。"""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="需要管理员令牌")
    with _lock:
        cursor = _conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="订单不存在")
    return {"id": order_id, "deleted": True}


@app.get(
    "/api/v1/admin/orders/export",
    summary="导出全部订单(管理员)",
    tags=["admin"],
)
def export_orders(
    x_admin_token: str | None = Header(None, alias="X-Admin-Token", description="管理员令牌"),
) -> dict[str, Any]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, item_id, quantity, amount, status, remark, created_at FROM orders"
        ).fetchall()
    orders = [_row_to_dict(row) for row in rows]
    return {"total": len(orders), "orders": orders}


@app.post("/internal/reset", include_in_schema=False)
def reset_state(
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
) -> dict[str, Any]:
    """测试辅助接口: 把数据恢复到初始状态 (不出现在 OpenAPI 文档里)。"""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="需要管理员令牌")
    reset_demo_data()
    with _lock:
        count = _conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    return {"reset": True, "orders": int(count)}
