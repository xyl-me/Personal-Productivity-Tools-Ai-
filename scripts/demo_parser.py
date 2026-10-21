"""M0 冒烟脚本: 解析靶场文档, 打印渲染出的 Prompt 素材。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.parser import parse_spec_dict, render_api_catalog, render_endpoint_markdown  # noqa: E402
from target_service.main import app  # noqa: E402

DEMO_ENDPOINTS = ("POST /api/v1/orders", "GET /api/v1/orders/search")


def main() -> None:
    spec = parse_spec_dict(app.openapi())

    print(render_api_catalog(spec))
    print()

    index = spec.endpoint_index()
    for uid in DEMO_ENDPOINTS:
        print(render_endpoint_markdown(index[uid]))
        print()

    node_free = spec.find("GET", "/api/v1/orders/{order_id}")
    assert node_free is not None
    print(f"[解析摘要] 接口数={len(spec.endpoints)} 格式={spec.spec_format} "
          f"版本={spec.spec_version} 结构数={len(spec.schema_names)}")
    print(f"[解析摘要] GET /orders/{{order_id}} 参数: "
          + ", ".join(p.signature() for p in node_free.params))


if __name__ == "__main__":
    main()
