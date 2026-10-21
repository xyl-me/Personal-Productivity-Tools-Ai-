"""M2 冒烟脚本: 生成用例 -> 校验 -> 保存 -> 执行 -> 评测。"""

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
from app.executor.runner import load_case_suite, run_cases, save_case_suite  # noqa: E402
from app.executor.triage import evaluate  # noqa: E402
from app.generator.cache import GenerationCache  # noqa: E402
from app.generator.client import build_client  # noqa: E402
from app.generator.context import RuntimeContext  # noqa: E402
from app.generator.engine import run_generation, to_suite  # noqa: E402
from app.parser import parse_spec_dict  # noqa: E402
from target_service.main import app, reset_demo_data  # noqa: E402

BASELINE_CASES = ROOT / "cases" / "order_service_cases.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M2: AI 用例生成与评测")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true", help="离线回放 fixtures (不调用模型)")
    group.add_argument("--real", action="store_true", help="真实调用 DeepSeek")
    parser.add_argument("--endpoints", nargs="*", default=None, help="只生成指定接口")
    parser.add_argument("--count", type=int, default=6, help="每个接口生成多少条")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--no-cache", action="store_true", help="禁用提示词缓存")
    parser.add_argument(
        "--context",
        default=str(ROOT / "data" / "runtime.json"),
        help="运行时上下文 (凭据/已知资源 ID), 默认 data/runtime.json",
    )
    parser.add_argument("--no-context", action="store_true", help="不注入运行时上下文")
    parser.add_argument("--out", default=str(ROOT / "cases" / "ai_generated.json"))
    parser.add_argument("--no-execute", action="store_true", help="只生成不执行")
    return parser.parse_args(argv)


def print_endpoint_table(run) -> None:
    print("| 接口 | 用例 | 尝试 | 拦截字段 | 拒绝 | 异常 | 耗时(ms) |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for item in run.endpoints:
        dropped = len(item.dropped_fields)
        print(
            f"| `{item.endpoint}` | {item.accepted_count} | {item.attempts} | "
            f"{dropped} | {len(item.rejected)} | {len(item.parse_errors)} | "
            f"{item.duration_ms:.0f} |"
        )
        for error in item.parse_errors:
            print(f"  - {error}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)
    mode = "mock" if args.mock else ("real" if args.real else "auto")

    spec = parse_spec_dict(app.openapi())
    client = build_client(mode=mode)

    print(f"模式: {client.mode} / 模型: {client.model}")
    if client.mode == "mock":
        print("注意: MOCK 模式回放的是手写 fixtures, 只验证流水线, 不产出质量指标。")
        print("      真实指标请配置 DEEPSEEK_API_KEY 后跑 --real。")
        if args.endpoints is None:
            args.endpoints = client.endpoints
            print(f"      mock 只回放 fixtures 覆盖的 {len(args.endpoints)} 个接口。")
    print()

    cache = None if args.no_cache else GenerationCache()
    runtime_context = None if args.no_context else RuntimeContext.load(args.context)
    if runtime_context is not None and runtime_context.empty:
        print("运行时上下文为空: 凭据/已知 ID 不会被注入, 期望成功的用例可能因 401/404 失败。")
        print(f"  可编辑 {args.context} 补充 headers / path_params\n")
    elif runtime_context is not None:
        print(
            f"运行时上下文已加载: 凭据 {len(runtime_context.headers)} 个, "
            f"已知路径参数 {len(runtime_context.path_params)} 个\n"
        )

    run = run_generation(
        spec,
        client,
        endpoints=args.endpoints,
        count=args.count,
        max_concurrency=args.concurrency,
        cache=cache,
        context=runtime_context,
    )

    print(run.summary())
    print()
    print_endpoint_table(run)

    suite = to_suite(run, name=f"ai_generated_{client.mode}")
    if not suite.cases:
        print("\n没有生成任何用例, 退出。")
        return 1
    out = save_case_suite(suite, args.out)
    print(f"\n用例已保存: {out} ({len(suite)} 条)")

    if args.no_execute:
        return 0

    print("\n== 执行 ==")
    with live_server(app) as base_url:
        report = run_cases(suite, base_url=base_url, reset=reset_demo_data)
    print(report.summary_table())

    evaluation = evaluate(suite, report, GROUND_TRUTH)
    print()
    print(evaluation.render())

    if client.mode == "mock":
        print("\n[MOCK] 上面的召回率来自手写 fixtures, 不具备质量参考价值。")
        return 0

    print("\n== 与手写基线对比 ==")
    baseline_suite = load_case_suite(BASELINE_CASES)
    with live_server(app) as base_url:
        baseline_report = run_cases(baseline_suite, base_url=base_url, reset=reset_demo_data)
    baseline = evaluate(baseline_suite, baseline_report, GROUND_TRUTH)

    print("| 指标 | 手写基线 | AI 生成 |")
    print("| --- | --- | --- |")
    print(f"| 用例数 | {len(baseline_suite)} | {len(suite)} |")
    print(f"| 缺陷召回 | {baseline.detected}/{baseline.total_defects} | "
          f"{evaluation.detected}/{evaluation.total_defects} |")
    print(f"| 误报率 | {baseline.false_positive_rate:.1%} | "
          f"{evaluation.false_positive_rate:.1%} |")
    print(f"| 未归因失败 | {len(baseline.unattributed)} | {len(evaluation.unattributed)} |")
    print(f"| 幻觉字段 | 0 (人工编写) | {len(run.dropped_fields)} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
