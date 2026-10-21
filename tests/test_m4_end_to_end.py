"""M4 端到端: 导出物真的能用, 而且结论和平台完全一致。"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.executor.ground_truth import GROUND_TRUTH
from app.executor.live import live_server
from app.executor.runner import load_case_suite, run_cases
from app.exporter.excel import export_workbook
from app.exporter.pytest_script import export_pytest
from app.parser import parse_spec_dict
from app.pipeline import export_all, score_suite, triage_suite
from target_service.main import ADMIN_TOKEN, app, reset_demo_data

ROOT = Path(__file__).parents[1]
SUITE_PATH = ROOT / "cases" / "order_service_cases.json"
SPEC = parse_spec_dict(app.openapi())


@pytest.fixture(scope="module")
def executed():
    reset_demo_data()
    suite = load_case_suite(SUITE_PATH)
    with live_server(app) as base_url:
        report = run_cases(suite, base_url=base_url, reset=reset_demo_data)
    return suite, report


def _failed_ids(junit: Path) -> set[str]:
    tree = ElementTree.parse(junit)
    failed: set[str] = set()
    for testcase in tree.iter("testcase"):
        if any(child.tag in ("failure", "error") for child in testcase):
            name = testcase.get("name", "")
            failed.add(name.split("[")[-1].rstrip("]"))
    return failed


def _run_exported_script(script: Path, junit: Path, base_url: str) -> subprocess.CompletedProcess:
    reset_demo_data()
    env = dict(
        os.environ,
        BASE_URL=base_url,
        CASE_RESET_URL=f"{base_url}/internal/reset",
        CASE_RESET_TOKEN=ADMIN_TOKEN,
        PYTHONPATH=str(ROOT),
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(script),
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_exported_script_runs_standalone_and_matches_platform(executed, tmp_dir: Path) -> None:
    suite, report = executed
    script = export_pytest(tmp_dir / "test_exported.py", suite.cases).path
    junit = tmp_dir / "junit.xml"

    with live_server(app) as base_url:
        completed = _run_exported_script(script, junit, base_url)

    assert junit.exists(), completed.stdout + completed.stderr
    script_failed = _failed_ids(junit)
    platform_failed = {item.case_id for item in report.failures()}

    assert script_failed == platform_failed
    assert script_failed
    assert completed.returncode != 0


def test_exported_script_passes_on_a_clean_assertion_set(tmp_dir: Path) -> None:
    """只保留正向用例时, 导出的脚本应该全绿 —— 证明它不是"永远失败"。"""
    suite = load_case_suite(SUITE_PATH)
    positives = [case for case in suite.cases if case.is_positive]
    assert positives

    script = export_pytest(tmp_dir / "test_positive.py", positives).path
    junit = tmp_dir / "junit_ok.xml"
    with live_server(app) as base_url:
        completed = _run_exported_script(script, junit, base_url)

    assert completed.returncode == 0, completed.stdout[-2000:]
    assert _failed_ids(junit) == set()


def test_excel_has_four_sheets_and_real_content(executed, tmp_dir: Path) -> None:
    suite, report = executed
    evaluation = triage_suite(suite, report, GROUND_TRUTH)
    score = score_suite(SPEC, suite)

    path = export_workbook(
        tmp_dir / "report.xlsx",
        suite=suite,
        report=report,
        evaluation=evaluation,
        score=score,
    )

    with zipfile.ZipFile(path) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        summary = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

    for name in ("汇总", "用例明细", "质量评分", "缺陷归因"):
        assert name in workbook
    assert "order_service_handwritten" in summary
    assert "缺陷召回" in summary
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            if member.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(member))


def test_export_all_writes_three_artifacts(executed, tmp_dir: Path) -> None:
    suite, report = executed
    artifacts = export_all(
        tmp_dir / "out",
        spec=SPEC,
        suite=suite,
        report=report,
        evaluation=triage_suite(suite, report, GROUND_TRUTH),
        score=score_suite(SPEC, suite),
        base_url_hint="http://127.0.0.1:9000",
    )

    items = artifacts.items()
    assert set(items) == {"excel", "pytest_script", "cases_json"}
    for path in items.values():
        assert path.exists() and path.stat().st_size > 0

    assert "order_service_handwritten" in artifacts.cases_json.read_text(encoding="utf-8")
    script = artifacts.pytest_script.read_text(encoding="utf-8")
    assert "BASE_URL" in script and "http://127.0.0.1:9000" in script


def test_streamlit_app_is_importable() -> None:
    """界面必须能在不启动服务的情况下被导入 —— 否则链路逻辑就锁死在回调里了。"""
    import importlib

    module = importlib.import_module("app.ui.streamlit_app")
    assert callable(module.main)
