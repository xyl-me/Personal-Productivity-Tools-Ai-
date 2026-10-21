"""生成两版文档快照, 用于演示"文档更新 -> 增量生成"。"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from target_service.main import app  # noqa: E402

OUT = ROOT / "fixtures" / "specs"


def build_versions() -> tuple[dict, dict]:
    v1 = copy.deepcopy(app.openapi())
    v2 = copy.deepcopy(v1)

    schemas = v2["components"]["schemas"]

    create = schemas["CreateOrderRequest"]["properties"]
    create["channel"] = {
        "title": "Channel",
        "type": "string",
        "enum": ["web", "app"],
        "description": "下单渠道",
    }

    create["remark"]["maxLength"] = 120

    schemas["CreateOrderRequest"]["properties"]["quantity"]["minimum"] = 1

    for param in v2["paths"]["/api/v1/orders"]["get"]["parameters"]:
        if param["name"] == "page_size":
            param["schema"]["maximum"] = 50

    v2["paths"].pop("/api/v1/orders/search")

    schemas["UpdateRemarkRequest"] = {
        "title": "UpdateRemarkRequest",
        "type": "object",
        "required": ["remark"],
        "properties": {
            "remark": {"title": "Remark", "type": "string", "maxLength": 20, "description": "新备注"}
        },
    }
    v2["paths"]["/api/v1/orders/{order_id}/remark"] = {
        "patch": {
            "tags": ["orders"],
            "summary": "更新订单备注",
            "operationId": "update_remark",
            "parameters": [
                {
                    "name": "order_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer", "title": "Order Id"},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/UpdateRemarkRequest"}}
                },
            },
            "responses": {
                "200": {"description": "Successful Response", "content": {"application/json": {}}}
            },
        }
    }
    return v1, v2


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    v1, v2 = build_versions()
    for name, document in (("orders_v1.json", v1), ("orders_v2.json", v2)):
        path = OUT / name
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{path}  ({len(path.read_text(encoding='utf-8')) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
