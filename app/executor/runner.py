"""批量执行器。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse

import httpx

from .core import execute_case
from .models import CaseResult, ExecutionReport, TestCase, TestCaseSuite

ResetHook = Callable[[], None]

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback(url: str) -> bool:
    """判断目标是不是本机地址。"""
    host = (urlparse(url).hostname or "").lower()
    return host in LOOPBACK_HOSTS


def load_case_suite(path: str | Path) -> TestCaseSuite:
    """从 JSON 文件加载用例集。M2 生成的结果会落到同样的结构。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"用例文件不存在: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return TestCaseSuite(suite=file_path.stem, cases=data)
    return TestCaseSuite.model_validate(data)


def save_case_suite(suite: TestCaseSuite, path: str | Path) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(suite.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return file_path


def run_cases(
    cases: TestCaseSuite | Iterable[TestCase],
    *,
    base_url: str = "http://testserver",
    reset: ResetHook | None = None,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
    suite_name: str = "",
) -> ExecutionReport:
    """逐条执行用例。"""
    case_list = list(cases.cases if isinstance(cases, TestCaseSuite) else cases)
    if isinstance(cases, TestCaseSuite) and not suite_name:
        suite_name = cases.suite

    owns_client = client is None
    if client is None:
        # 本机地址必须绕过系统代理: VPN 开启后 Windows 的 ProxyServer 会把 127.0.0.1
        # 也送去代理, 直接返回 502, 看起来像"被测服务挂了"。
        client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=True,
            trust_env=not is_loopback(base_url),
        )

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = time.perf_counter()
    results: list[CaseResult] = []
    try:
        for case in case_list:
            if reset is not None:
                reset()
            results.append(execute_case(case, client))
    finally:
        if owns_client:
            client.close()

    return ExecutionReport(
        suite=suite_name,
        started_at=started_at,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        results=results,
    )
