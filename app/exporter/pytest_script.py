"""Pytest 脚本导出。"""

from __future__ import annotations

import pprint
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.executor.models import TestCase

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "cases_test.py.j2"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
    autoescape=False,
)


def case_payload(case: TestCase) -> dict[str, Any]:
    """把用例摊平成脚本里嵌入的纯数据 (只保留执行需要的东西)。"""
    return {
        "id": case.id,
        "name": case.name,
        "endpoint": case.endpoint,
        "method": case.method,
        "path": case.path,
        "query": case.query or {},
        "headers": case.headers or {},
        "body": case.body,
        "expected": {
            "status": case.expect.statuses,
            "json_contains": case.expect.json_contains,
            "json_length": case.expect.json_length,
            "body_contains": case.expect.body_contains,
        },
    }


def render_pytest(
    cases: Sequence[TestCase],
    *,
    title: str = "接口自动化测试",
    source: str = "手写用例",
    base_url_hint: str = DEFAULT_BASE_URL,
    filename: str = "test_generated.py",
) -> str:
    if not cases:
        raise ValueError("没有用例可导出")

    payload = [case_payload(case) for case in cases]
    cases_literal = pprint.pformat(payload, width=100, sort_dicts=False)
    return _env.get_template(TEMPLATE_NAME).render(
        title=title,
        source=source,
        case_count=len(cases),
        base_url=base_url_hint,
        filename=filename,
        cases_json=cases_literal,
        generated_at=datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
    )


@dataclass
class PytestExport:
    path: Path
    case_count: int

    def render_command(self, base_url: str = DEFAULT_BASE_URL) -> str:
        return f"BASE_URL={base_url} pytest {self.path.name} -v"


def export_pytest(
    path: str | Path,
    cases: Sequence[TestCase],
    *,
    title: str = "接口自动化测试",
    source: str = "手写用例",
    base_url_hint: str = DEFAULT_BASE_URL,
) -> PytestExport:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    code = render_pytest(
        cases,
        title=title,
        source=source,
        base_url_hint=base_url_hint,
        filename=target.name,
    )
    target.write_text(code, encoding="utf-8")
    return PytestExport(path=target, case_count=len(cases))
