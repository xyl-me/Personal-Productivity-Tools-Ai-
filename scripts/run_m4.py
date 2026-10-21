"""M4 冒烟脚本: 生成/加载用例 -> 执行 -> 归因 -> 评测 -> 导出 Excel + Pytest 脚本。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from app.executor.ground_truth import GROUND_TRUTH  # noqa: E402
from app.executor.live import live_server  # noqa: E402
from app.executor.runner import load_case_suite  # noqa: E402
from app.generator.client import build_client  # noqa: E402
from app.parser import parse_spec_dict  # noqa: E402
from app.pipeline import (  # noqa: E402
    execute_suite,
    export_all,
    generate_suite,
    score_suite,
    triage_suite,
)
from target_service.main import app, reset_demo_data  # noqa: E402

HANDWRITTEN = ROOT / "cases" / "order_service_cases.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M4: 导出 Excel 用例表与 Pytest 脚本")
    parser.add_argument("--generate", action="store_true", help="用 AI 生成用例 (否则用手写用例集)")
    parser.add_argument("--real", action="store_true", help="生成时强制真实调用")
    parser.add_argument("--mock", action="store_true", help="生成时强制离线回放")
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--endpoints", nargs="*", default=None)
    parser.add_argument("--threshold", type=float, default=80.0)
    parser.add_argument("--out", default=str(ROOT / "reports"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    spec = parse_spec_dict(app.openapi())
    generation = None

    if args.generate:
        mode = "mock" if args.mock else ("real" if args.real else "auto")
        client = build_client(mode=mode)
        if client.mode == "mock" and args.endpoints is None:
            args.endpoints = client.endpoints
        generation, suite = generate_suite(
            spec, client, count=args.count, endpoints=args.endpoints
        )
        print(f"生成模式: {client.mode}")
    else:
        suite = load_case_suite(HANDWRITTEN)
        print(f"用例集: {suite.suite}")

    print(f"用例数: {len(suite)}\n")

    with live_server(app) as base_url:
        print(f"靶场已启动: {base_url}")
        report = execute_suite(suite, base_url=base_url, reset=reset_demo_data)

    evaluation = triage_suite(suite, report, GROUND_TRUTH)
    score = score_suite(spec, suite, threshold=args.threshold)

    print()
    print(report.summary_table())
    print()
    print(evaluation.render())
    print()
    print(score.render())

    if generation is not None:
        print()
        print("## 生成统计\n")
        print(generation.summary())

    artifacts = export_all(
        args.out,
        spec=spec,
        suite=suite,
        report=report,
        evaluation=evaluation,
        score=score,
        base_url_hint=base_url,
    )

    print("\n## 导出产物\n")
    for name, path in artifacts.items().items():
        size = path.stat().st_size
        print(f"- {name}: {path}  ({size / 1024:.1f} KB)")
    print()
    print("运行导出的脚本 (重置走 HTTP, 脚本与服务在不同进程):")
    print(f"  $env:BASE_URL='{base_url}'")
    print(f"  $env:CASE_RESET_URL='{base_url}/internal/reset'")
    print("  $env:CASE_RESET_TOKEN='secret-admin-token'")
    print(f"  pytest {artifacts.pytest_script}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
