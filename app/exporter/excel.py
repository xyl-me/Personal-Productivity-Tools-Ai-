"""Excel 导出: 四张表。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app.executor.models import CaseResult, ExecutionReport, TestCase, TestCaseSuite
from app.executor.triage import DefectEvaluation

from .xlsx import STYLE_WARN, Sheet, write_workbook

REVIEW_MARK = "需人工复核"

_CATEGORY_LABELS = {
    "positive": "正向",
    "boundary": "边界值",
    "equivalence": "等价类",
    "error_guess": "错误推测",
    "security": "安全",
    "other": "其他",
    "control": "对照",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _request_text(case: TestCase) -> str:
    text = f"{case.method} {case.path}"
    if case.query:
        text += "?" + "&".join(f"{key}={value}" for key, value in case.query.items())
    return text


def _body_text(case: TestCase) -> str:
    if case.body is None:
        return ""
    if isinstance(case.body, (dict, list)):
        import json

        return json.dumps(case.body, ensure_ascii=False)
    return str(case.body)


def _labels(case: TestCase) -> str:
    return ", ".join(_CATEGORY_LABELS.get(tag, tag) for tag in case.tags) or "-"


def _summary_sheet(
    suite: TestCaseSuite,
    report: ExecutionReport | None,
    score: Any | None,
    evaluation: DefectEvaluation | None,
) -> Sheet:
    rows: list[list[Any]] = [
        ["生成时间", _now()],
        ["用例集", suite.suite],
        ["用例总数", len(suite)],
    ]
    if report is not None:
        rows += [
            ["执行通过", report.passed],
            ["执行失败", report.failed],
            ["通过率", f"{report.pass_rate:.1%}"],
            ["执行耗时(ms)", report.duration_ms],
        ]
    if evaluation is not None:
        rows += [
            ["缺陷召回", f"{evaluation.detected}/{evaluation.total_defects} ({evaluation.recall:.0%})"],
            ["误报率", f"{evaluation.false_positive_rate:.1%}"],
            ["未归因失败", len(evaluation.unattributed)],
            ["重复命中", evaluation.duplicate_hits],
        ]
    if score is not None:
        rows += [
            ["接口数", len(score.endpoints)],
            ["平均质量分", f"{score.mean_score:.1f}"],
            ["通过评审", f"{len(score.passed)}/{len(score.endpoints)}"],
        ]
        if score.scenario_recall is not None:
            rows.append(["标注场景召回", f"{score.scenario_recall:.1%}"])
    rows.append(
        ["说明", "缺陷召回/误报率基于靶场预埋缺陷; 质量分基于规则子分, 未配置 API Key 时不含 LLM 评审"]
    )
    return Sheet(name="汇总", headers=["项目", "值"], rows=rows, widths=[22, 72])


def _case_sheet(
    suite: TestCaseSuite,
    results: dict[str, CaseResult] | None,
) -> Sheet:
    rows: list[list[Any]] = []
    styles: list[int] = []
    for case in suite.cases:
        result = (results or {}).get(case.id)
        actual = result.http_status if result and result.http_status is not None else ""
        outcome = ""
        if result is not None:
            outcome = "通过" if result.passed else ("执行异常" if result.status.value == "error" else "失败")
        rows.append(
            [
                case.id,
                case.endpoint,
                case.method,
                _request_text(case),
                case.name,
                _labels(case),
                "/".join(str(code) for code in case.expect.statuses),
                actual,
                outcome,
                result.failure_kind.value if result and result.failure_kind else "",
                result.duration_ms if result else "",
                _body_text(case),
                (result.error if result and result.error else case.description) or "",
            ]
        )
        styles.append(STYLE_WARN if result is not None and result.failed else 0)

    return Sheet(
        name="用例明细",
        headers=[
            "用例ID",
            "接口",
            "方法",
            "请求",
            "用例名",
            "分类",
            "期望状态",
            "实际状态",
            "结果",
            "失败类型",
            "耗时(ms)",
            "请求体",
            "说明",
        ],
        rows=rows,
        widths=[26, 34, 8, 46, 34, 16, 10, 10, 10, 18, 10, 40, 40],
        row_styles=styles,
    )


def _score_sheet(score: Any) -> Sheet:
    rows: list[list[Any]] = []
    styles: list[int] = []
    for item in score.endpoints:
        scenario = f"{item.scenario.covered}/{item.scenario.total}" if item.scenario else "-"
        llm = f"{item.llm.score}" if item.llm else ("失败" if item.llm_error else "-")
        rows.append(
            [
                item.endpoint,
                item.case_count,
                item.rule.coverage_score,
                item.rule.case_quality_score,
                item.rule.rule_score,
                scenario,
                llm,
                item.final_score,
                item.verdict,
                "; ".join(item.review_reasons[:4]),
            ]
        )
        styles.append(0 if item.passes else STYLE_WARN)

    return Sheet(
        name="质量评分",
        headers=[
            "接口",
            "用例数",
            "覆盖分",
            "用例质量",
            "规则分",
            "场景覆盖",
            "LLM分",
            "最终分",
            "结论",
            "复核原因",
        ],
        rows=rows,
        widths=[34, 8, 10, 10, 10, 12, 8, 10, 14, 70],
        row_styles=styles,
    )


def _defect_sheet(evaluation: DefectEvaluation) -> Sheet:
    rows: list[list[Any]] = []
    styles: list[int] = []
    for defect in evaluation.defects:
        hits = evaluation.hits.get(defect.id, [])
        rows.append(
            [
                defect.id,
                defect.title,
                defect.category,
                defect.endpoint,
                ", ".join(hits) if hits else "-",
                "命中" if hits else "漏测",
            ]
        )
        styles.append(0 if hits else STYLE_WARN)

    return Sheet(
        name="缺陷归因",
        headers=["缺陷", "描述", "类别", "接口", "命中用例", "结果"],
        rows=rows,
        widths=[8, 30, 20, 36, 34, 10],
        row_styles=styles,
    )


def export_workbook(
    path: str | Path,
    *,
    suite: TestCaseSuite,
    report: ExecutionReport | None = None,
    score: Any | None = None,
    evaluation: DefectEvaluation | None = None,
) -> Path:
    """导出四张表到一个 .xlsx。缺少哪一路数据就少哪张表。"""
    sheets: list[Sheet] = [_summary_sheet(suite, report, score, evaluation)]

    results = {item.case_id: item for item in report.results} if report is not None else None
    sheets.append(_case_sheet(suite, results))

    if score is not None:
        sheets.append(_score_sheet(score))
    if evaluation is not None:
        sheets.append(_defect_sheet(evaluation))

    return write_workbook(path, sheets)


def suite_from_cases(cases: Sequence[TestCase], *, name: str) -> TestCaseSuite:
    return TestCaseSuite(suite=name, cases=list(cases))
