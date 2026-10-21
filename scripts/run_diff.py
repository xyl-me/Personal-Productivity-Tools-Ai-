"""M6 冒烟: 文档两版对比 -> 只对变更接口生成 -> 变更点覆盖检查 -> 节省量化。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from app.evaluator.changes import evaluate_change_coverage  # noqa: E402
from app.executor.runner import save_case_suite  # noqa: E402
from app.generator.client import build_client  # noqa: E402
from app.generator.context import RuntimeContext  # noqa: E402
from app.generator.engine import run_generation, to_suite  # noqa: E402
from app.generator.incremental import (  # noqa: E402
    build_plan,
    summarize_changes,
    tag_incremental,
)
from app.generator.prompts import build_user_prompt  # noqa: E402
from app.parser import parse_spec_file  # noqa: E402

V1 = ROOT / "fixtures" / "specs" / "orders_v1.json"
V2 = ROOT / "fixtures" / "specs" / "orders_v2.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="文档 diff 与增量生成")
    parser.add_argument("--old", default=str(V1))
    parser.add_argument("--new", default=str(V2))
    parser.add_argument("--generate", action="store_true", help="对变更接口生成用例")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--out", default=str(ROOT / "cases" / "incremental.json"))
    return parser.parse_args(argv)


def prompt_chars(spec, uids, count: int, context: RuntimeContext | None) -> int:
    index = spec.endpoint_index()
    return sum(len(build_user_prompt(index[uid], count=count, context=context)) for uid in uids if uid in index)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    old_spec = parse_spec_file(args.old)
    new_spec = parse_spec_file(args.new)
    plan = build_plan(old_spec, new_spec)
    diff = plan.diff

    print(f"旧版: {args.old} ({len(old_spec.endpoints)} 个接口)")
    print(f"新版: {args.new} ({len(new_spec.endpoints)} 个接口)\n")

    print("## 变更清单\n")
    print(diff.render())
    print()

    if plan.removed:
        print("## 已下线接口 (不生成用例, 需确认调用方)\n")
        for uid in plan.removed:
            print(f"- `{uid}`")
        print()

    print("## 需要重新生成的接口\n")
    print("\n".join(summarize_changes(diff)) or "- 无")
    print()

    context = RuntimeContext.load()
    all_uids = [endpoint.uid for endpoint in new_spec.endpoints]
    full_chars = prompt_chars(new_spec, all_uids, args.count, context)
    inc_chars = prompt_chars(new_spec, plan.endpoints, args.count, context)
    print("## 生成量对比 (按 Prompt 字符数, 确定性可复现)\n")
    print("| 方案 | 接口数 | Prompt 字符 | 相对成本 |")
    print("| --- | --- | --- | --- |")
    print(f"| 全量 | {len(all_uids)} | {full_chars:,} | 100% |")
    print(f"| 增量 | {len(plan.endpoints)} | {inc_chars:,} | {inc_chars / full_chars:.0%} |")
    print(f"\n节省 {1 - inc_chars / full_chars:.0%} 的调用成本。")

    if not args.generate:
        print("\n(加 --generate 对变更接口真实生成用例)")
        return 0

    mode = "mock" if args.mock else ("real" if args.real else "auto")
    client = build_client(mode=mode)
    print(f"\n## 增量生成 (模式 {client.mode})\n")

    run = run_generation(
        new_spec,
        client,
        endpoints=plan.endpoints,
        count=args.count,
        context=context,
    )
    print(run.summary())
    print()
    print("| 接口 | 用例 | 尝试 | 拦截字段 | 异常 |")
    print("| --- | --- | --- | --- | --- |")
    for item in run.endpoints:
        print(
            f"| `{item.endpoint}` | {item.accepted_count} | {item.attempts} | "
            f"{len(item.dropped_fields)} | {len(item.parse_errors)} |"
        )
        for error in item.parse_errors:
            print(f"  - {error}")

    suite = tag_incremental(to_suite(run, name=f"incremental_{client.mode}"), diff)
    if not suite.cases:
        print("\n没有生成任何用例, 退出。")
        return 1
    out = save_case_suite(suite, args.out)
    print(f"\n用例已保存: {out} ({len(suite)} 条, 均带 incremental 标签)")

    print("\n## 变更点覆盖 (增量生成的质量门禁)\n")
    print(evaluate_change_coverage(suite, diff, new_spec).render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
