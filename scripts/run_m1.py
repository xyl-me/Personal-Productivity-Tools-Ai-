"""M1 冒烟脚本: 起真实靶场 -> 跑手写用例 -> 缺陷归集 -> 打印指标。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.executor.ground_truth import GROUND_TRUTH  # noqa: E402
from app.executor.live import live_server  # noqa: E402
from app.executor.runner import load_case_suite, run_cases  # noqa: E402
from app.executor.triage import evaluate  # noqa: E402
from target_service.main import app, reset_demo_data  # noqa: E402

CASES = ROOT / "cases" / "order_service_cases.json"


def main() -> None:
    suite = load_case_suite(CASES)
    print(f"用例集: {suite.suite} ({len(suite)} 条)")

    with live_server(app) as base_url:
        print(f"靶场已启动: {base_url}")
        report = run_cases(suite, base_url=base_url, reset=reset_demo_data)

    print()
    print(report.summary_table())
    print()
    print(evaluate(suite, report, GROUND_TRUTH).render())


if __name__ == "__main__":
    main()
