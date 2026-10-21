"""把各层串成一条可复用的流水线。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from app.evaluator.judge import JudgeVerdict
from app.evaluator.rules import evaluate_rules
from app.evaluator.scenarios import load_labeled_scenarios
from app.evaluator.scoring import SuiteScore, evaluate_suite
from app.executor.models import ExecutionReport, TestCase, TestCaseSuite
from app.executor.runner import run_cases
from app.executor.triage import DefectEvaluation, evaluate
from app.exporter.excel import export_workbook
from app.exporter.pytest_script import export_pytest
from app.generator.cache import GenerationCache
from app.generator.client import LLMClient
from app.generator.context import RuntimeContext
from app.generator.engine import GenerationRun, run_generation, to_suite
from app.parser.models import ApiSpec


@dataclass
class PipelineArtifacts:
    excel: Path | None = None
    pytest_script: Path | None = None
    cases_json: Path | None = None

    def items(self) -> dict[str, Path]:
        return {
            name: path
            for name, path in (
                ("excel", self.excel),
                ("pytest_script", self.pytest_script),
                ("cases_json", self.cases_json),
            )
            if path is not None
        }


@dataclass
class PipelineResult:
    spec: ApiSpec
    suite: TestCaseSuite
    generation: GenerationRun | None = None
    report: ExecutionReport | None = None
    evaluation: DefectEvaluation | None = None
    score: SuiteScore | None = None
    artifacts: PipelineArtifacts = field(default_factory=PipelineArtifacts)


def generate_suite(
    spec: ApiSpec,
    client: LLMClient,
    *,
    count: int = 6,
    endpoints: Sequence[str] | None = None,
    max_concurrency: int = 4,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
    suite_name: str | None = None,
    runtime_context: RuntimeContext | None = None,
) -> tuple[GenerationRun, TestCaseSuite]:
    cache = GenerationCache(cache_path) if use_cache else None
    run = run_generation(
        spec,
        client,
        endpoints=endpoints,
        count=count,
        max_concurrency=max_concurrency,
        cache=cache,
        context=runtime_context if runtime_context is not None else RuntimeContext.load(),
    )
    suite = to_suite(run, name=suite_name or f"ai_generated_{client.mode}")
    return run, suite


def execute_suite(
    suite: TestCaseSuite,
    *,
    base_url: str,
    reset: Callable[[], None] | None = None,
    timeout: float = 10.0,
) -> ExecutionReport:
    return run_cases(suite, base_url=base_url, reset=reset, timeout=timeout)


def score_suite(
    spec: ApiSpec,
    suite: TestCaseSuite,
    *,
    threshold: float = 80.0,
    llm_weight: float = 0.3,
    verdicts: dict[str, JudgeVerdict | None] | None = None,
    judge_errors: dict[str, str | None] | None = None,
    use_labels: bool = True,
) -> SuiteScore:
    scenarios = load_labeled_scenarios() if use_labels else None
    return evaluate_suite(
        spec,
        suite,
        scenarios=scenarios,
        verdicts=verdicts or {},
        judge_errors=judge_errors or {},
        threshold=threshold,
        llm_weight=llm_weight,
    )


def triage_suite(
    suite: TestCaseSuite,
    report: ExecutionReport,
    ground_truth: Sequence[Any],
) -> DefectEvaluation:
    return evaluate(suite, report, ground_truth)


def export_all(
    out_dir: str | Path,
    *,
    spec: ApiSpec,
    suite: TestCaseSuite,
    report: ExecutionReport | None = None,
    evaluation: DefectEvaluation | None = None,
    score: SuiteScore | None = None,
    base_url_hint: str = "http://127.0.0.1:8000",
    title: str | None = None,
) -> PipelineArtifacts:
    """一次导出 Excel + Pytest 脚本 + 用例 JSON。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = suite.suite or "cases"
    label = title or f"{spec.title or 'API'} 接口测试"

    artifacts = PipelineArtifacts()
    artifacts.excel = export_workbook(
        directory / f"{stem}.xlsx",
        suite=suite,
        report=report,
        score=score,
        evaluation=evaluation,
    )
    artifacts.pytest_script = export_pytest(
        directory / f"test_{stem}.py",
        suite.cases,
        title=label,
        source=suite.suite,
        base_url_hint=base_url_hint,
    ).path
    artifacts.cases_json = directory / f"{stem}.json"
    artifacts.cases_json.write_text(
        suite.model_dump_json(indent=2), encoding="utf-8"
    )
    return artifacts


def run_pipeline(
    spec: ApiSpec,
    client: LLMClient,
    *,
    out_dir: str | Path,
    base_url: str | None = None,
    reset: Callable[[], None] | None = None,
    endpoints: Sequence[str] | None = None,
    count: int = 6,
    threshold: float = 80.0,
    ground_truth: Sequence[Any] = (),
    titles: dict[str, str] | None = None,
) -> PipelineResult:
    """生成 -> 执行 -> 归因 -> 评测 -> 导出, 一步到位。"""
    generation, suite = generate_suite(spec, client, count=count, endpoints=endpoints)
    result = PipelineResult(spec=spec, suite=suite, generation=generation)

    if base_url and suite.cases:
        result.report = execute_suite(suite, base_url=base_url, reset=reset)
        if ground_truth:
            result.evaluation = triage_suite(suite, result.report, ground_truth)

    if suite.cases:
        result.score = score_suite(spec, suite, threshold=threshold)
        result.artifacts = export_all(
            out_dir,
            spec=spec,
            suite=suite,
            report=result.report,
            evaluation=result.evaluation,
            score=result.score,
            base_url_hint=base_url or "http://127.0.0.1:8000",
            title=(titles or {}).get(suite.suite),
        )
    return result


def rule_only_report(spec: ApiSpec, suite: TestCaseSuite) -> list[dict[str, Any]]:
    """按接口给出规则分的明细, 供界面上展开查看。"""
    grouped: dict[str, list[TestCase]] = {}
    for case in suite.cases:
        grouped.setdefault(case.endpoint, []).append(case)

    rows: list[dict[str, Any]] = []
    for endpoint in spec.endpoints:
        cases = grouped.get(endpoint.uid, [])
        if not cases:
            continue
        evaluation = evaluate_rules(endpoint, cases)
        rows.append(
            {
                "endpoint": endpoint.uid,
                "cases": len(cases),
                "score": evaluation.rule_score,
                "missed": [f"{item.title}: {item.note}" for item in evaluation.missed],
            }
        )
    return rows
