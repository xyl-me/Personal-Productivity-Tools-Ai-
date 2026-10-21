"""靶场服务契约测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.parser import parse_spec_dict
from target_service.main import ADMIN_TOKEN, app, reset_demo_data

API = "/api/v1"


@pytest.fixture()
def client() -> TestClient:
    reset_demo_data()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_valid_request_returns_201(client: TestClient) -> None:
    response = client.post(
        f"{API}/orders",
        json={"item_id": "SKU-9", "quantity": 2, "amount": 39.9, "remark": "ok"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["item_id"] == "SKU-9"
    assert body["status"] == "CREATED"


def test_missing_required_field_returns_422(client: TestClient) -> None:
    """对照组: 必填缺失由 Pydantic 正确拦截。"""
    response = client.post(f"{API}/orders", json={"quantity": 1, "amount": 1.0})
    assert response.status_code == 422


def test_list_orders_default_page(client: TestClient) -> None:
    response = client.get(f"{API}/orders")
    assert response.status_code == 200
    assert len(response.json()) == 3


def test_d1_long_remark_returns_500(client: TestClient) -> None:
    """文档声明 maxLength=200, 实现只接受 20, 21 字符即 500。"""
    response = client.post(
        f"{API}/orders",
        json={"item_id": "SKU-1", "quantity": 1, "amount": 1.0, "remark": "x" * 21},
    )
    assert response.status_code == 500


def test_d1_short_remark_is_accepted(client: TestClient) -> None:
    response = client.post(
        f"{API}/orders",
        json={"item_id": "SKU-1", "quantity": 1, "amount": 1.0, "remark": "x" * 20},
    )
    assert response.status_code == 201


@pytest.mark.parametrize("amount", [-1, 0, -0.01])
def test_d2_non_positive_amount_is_accepted(client: TestClient, amount: float) -> None:
    response = client.post(
        f"{API}/orders", json={"item_id": "SKU-1", "quantity": 1, "amount": amount}
    )
    assert response.status_code == 201
    assert response.json()["amount"] == amount


@pytest.mark.parametrize("quantity", [0, -3])
def test_d3_non_positive_quantity_is_accepted(client: TestClient, quantity: int) -> None:
    response = client.post(
        f"{API}/orders", json={"item_id": "SKU-1", "quantity": quantity, "amount": 1.0}
    )
    assert response.status_code == 201
    assert response.json()["quantity"] == quantity


def test_d4_sql_injection_returns_all_orders(client: TestClient) -> None:
    response = client.get(f"{API}/orders/search", params={"q": "' OR '1'='1"})
    assert response.status_code == 200
    assert len(response.json()) == 3


def test_d4_normal_search_still_works(client: TestClient) -> None:
    """对照组: 正常关键字检索结果正确, 说明注入判断不是靠"总是失败"。"""
    response = client.get(f"{API}/orders/search", params={"q": "SKU-1002"})
    assert len(response.json()) == 1


def test_d5_page_zero_repeats_first_page(client: TestClient) -> None:
    first = client.get(f"{API}/orders", params={"page": 1, "page_size": 2}).json()
    zero = client.get(f"{API}/orders", params={"page": 0, "page_size": 2}).json()
    assert zero == first


def test_d5_negative_page_size_returns_everything(client: TestClient) -> None:
    response = client.get(f"{API}/orders", params={"page": 1, "page_size": -1})
    assert len(response.json()) == 3


def test_d5_huge_page_size_is_not_capped(client: TestClient) -> None:
    response = client.get(f"{API}/orders", params={"page": 1, "page_size": 100000})
    assert len(response.json()) == 3


def test_d6_non_numeric_order_id_returns_500(client: TestClient) -> None:
    assert client.get(f"{API}/orders/abc").status_code == 500


def test_d6_missing_order_returns_404(client: TestClient) -> None:
    """对照组: 合法但不存在 -> 404。"""
    assert client.get(f"{API}/orders/99999").status_code == 404


def test_d7_invalid_status_is_persisted(client: TestClient) -> None:
    response = client.put(f"{API}/orders/1/status", json={"status": "HACKED"})
    assert response.status_code == 200
    assert client.get(f"{API}/orders/1").json()["status"] == "HACKED"


def test_d7_unknown_order_returns_404(client: TestClient) -> None:
    """对照组: 订单不存在 -> 404。"""
    assert client.put(f"{API}/orders/99999/status", json={"status": "PAID"}).status_code == 404


def test_d8_export_without_token_leaks_all_orders(client: TestClient) -> None:
    response = client.get(f"{API}/admin/orders/export")
    assert response.status_code == 200
    assert response.json()["total"] == 3


def test_delete_without_token_returns_401(client: TestClient) -> None:
    """对照组: 这个接口的鉴权是对的, 用来估算用例误报率。"""
    assert client.delete(f"{API}/orders/1").status_code == 401


def test_delete_with_token_returns_200(client: TestClient) -> None:
    response = client.delete(f"{API}/orders/1", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert response.status_code == 200
    assert client.get(f"{API}/orders/1").status_code == 404


def test_target_service_openapi_can_be_parsed() -> None:
    spec = parse_spec_dict(app.openapi())

    assert spec.title == "Demo Order Service"
    assert len(spec.endpoints) == 7
    assert {ep.uid for ep in spec.endpoints} == {
        "POST /api/v1/orders",
        "GET /api/v1/orders",
        "GET /api/v1/orders/search",
        "GET /api/v1/orders/{order_id}",
        "PUT /api/v1/orders/{order_id}/status",
        "DELETE /api/v1/orders/{order_id}",
        "GET /api/v1/admin/orders/export",
    }


def test_target_service_document_contract() -> None:
    spec = parse_spec_dict(app.openapi())

    create = spec.find("POST", "/api/v1/orders")
    assert create is not None and create.request_body is not None
    assert create.request_body.ref_name == "CreateOrderRequest"
    assert create.request_body.json_schema["required"] == ["item_id", "quantity", "amount"]
    assert create.request_body.json_schema["properties"]["remark"]["maxLength"] == 200

    get_one = spec.find("GET", "/api/v1/orders/{order_id}")
    assert get_one is not None
    order_id = get_one.param("order_id")
    assert order_id is not None and order_id.type == "string" and order_id.required is True

    export = spec.find("GET", "/api/v1/admin/orders/export")
    assert export is not None
    token = export.param("X-Admin-Token")
    assert token is not None and token.required is False

    update = spec.find("PUT", "/api/v1/orders/{order_id}/status")
    assert update is not None
    assert update.request_body is not None
    assert update.request_body.ref_name == "StatusUpdate"

    delete = spec.find("DELETE", "/api/v1/orders/{order_id}")
    assert delete is not None
    assert delete.param("X-Admin-Token") is not None
