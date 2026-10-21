"""Streamlit 界面。"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import streamlit as st
except ImportError:  # pragma: no cover - 取决于环境
    st = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.evaluator.judge import run_judging  # noqa: E402
from app.evaluator.scenarios import load_labeled_scenarios  # noqa: E402
from app.executor.ground_truth import GROUND_TRUTH  # noqa: E402
from app.executor.live import live_server  # noqa: E402
from app.executor.runner import load_case_suite  # noqa: E402
from app.generator.client import build_client  # noqa: E402
from app.parser import parse_spec_dict, render_api_catalog  # noqa: E402
from app.pipeline import (  # noqa: E402
    execute_suite,
    export_all,
    generate_suite,
    score_suite,
    triage_suite,
)

HANDWRITTEN = ROOT / "cases" / "order_service_cases.json"


def _target_app():
    """内置靶场应用; 用真实导入避免和主流程耦合。"""
    from target_service.main import app as target_app

    return target_app


def _reset_hook():
    from target_service.main import reset_demo_data

    return reset_demo_data


def _state() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "spec": None,
        "source": "",
        "suite": None,
        "generation": None,
        "report": None,
        "score": None,
        "evaluation": None,
        "base_url": "",
        "server": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    return st.session_state


def _section_source(state: dict[str, Any]) -> None:
    st.header("1. 接口文档")
    choice = st.radio(
        "文档来源",
        ["内置靶场 (Demo Order Service)", "上传 Swagger / OpenAPI 文件"],
        horizontal=True,
    )

    document: dict[str, Any] | None = None
    source = ""
    if choice.startswith("内置"):
        document = _target_app().openapi()
        source = "target_service.main:app"
    else:
        uploaded = st.file_uploader("选择 .json / .yaml 文件", type=["json", "yaml", "yml"])
        if uploaded is not None:
            text = uploaded.getvalue().decode("utf-8-sig")
            if uploaded.name.endswith((".yaml", ".yml")):
                import yaml

                document = yaml.safe_load(text)
            else:
                document = json.loads(text)
            source = uploaded.name

    if document is None:
        st.info("请先选择文档来源。")
        return

    try:
        spec = parse_spec_dict(document)
    except Exception as exc:  # noqa: BLE001 - 解析错误要展示给用户
        st.error(f"文档解析失败: {exc}")
        return

    state["spec"] = spec
    state["source"] = source
    st.success(f"解析成功: {spec.title} {spec.version} / {len(spec.endpoints)} 个接口")
    with st.expander("接口清单", expanded=False):
        st.markdown(render_api_catalog(spec))


def _section_generate(state: dict[str, Any]) -> None:
    spec = state["spec"]
    st.header("2. 生成用例")

    col1, col2, col3 = st.columns(3)
    mode_label = col1.selectbox("生成模式", ["离线回放 (mock)", "真实调用 DeepSeek"])
    per_endpoint = col2.number_input("每接口用例数", min_value=1, max_value=20, value=6)
    only_subset = col3.checkbox("只对部分接口生成", value=False)

    endpoints = None
    if only_subset:
        endpoints = st.multiselect(
            "选择接口", [endpoint.uid for endpoint in spec.endpoints], default=[]
        )

    use_handwritten = st.checkbox("直接加载手写用例集 (跳过生成)", value=False)

    if st.button("开始", type="primary", key="btn_generate"):
        if use_handwritten:
            state["suite"] = load_case_suite(HANDWRITTEN)
            state["generation"] = None
            st.success(f"已加载手写用例 {len(state['suite'])} 条")
        else:
            client = build_client(mode="real" if mode_label.startswith("真实") else "mock")
            with st.spinner(f"生成中 ({client.mode}) ..."):
                generation, suite = generate_suite(
                    spec,
                    client,
                    count=int(per_endpoint),
                    endpoints=endpoints or None,
                )
            state["generation"] = generation
            state["suite"] = suite
            st.success(f"生成 {len(suite)} 条用例")
            if generation.dropped_fields:
                st.warning(
                    "已拦截幻觉字段: " + ", ".join(sorted(set(generation.dropped_fields)))
                )

    suite = state["suite"]
    generation = state["generation"]
    if suite is None:
        return

    st.caption(f"当前用例集: {suite.suite} ({len(suite)} 条)")
    if generation is not None:
        st.code(generation.summary(), language="text")
    if suite.cases:
        st.dataframe(
            [
                {
                    "用例": case.id,
                    "接口": case.endpoint,
                    "名称": case.name,
                    "期望": "/".join(str(code) for code in case.expect.statuses),
                    "标签": ", ".join(case.tags),
                }
                for case in suite.cases
            ],
            use_container_width=True,
        )


def _section_execute(state: dict[str, Any]) -> None:
    st.header("3. 执行与被测服务")

    col1, col2 = st.columns([3, 1])
    base_url = col1.text_input("被测服务地址", value=state["base_url"] or "http://127.0.0.1:8000")
    state["base_url"] = base_url

    if col2.button("启动内置靶场"):
        server = live_server(_target_app())
        state["server"] = server
        state["base_url"] = server.__enter__()
        st.success(f"靶场已启动: {state['base_url']}")
        st.rerun()

    reset = _reset_hook() if state["base_url"] and state["source"].startswith("target_service") else None

    if st.button("执行用例", key="btn_execute") and state["suite"] is not None:
        with st.spinner("执行中 ..."):
            report = execute_suite(state["suite"], base_url=state["base_url"], reset=reset)
            state["report"] = report
            state["evaluation"] = triage_suite(state["suite"], report, GROUND_TRUTH)
            state["score"] = score_suite(state["spec"], state["suite"])
        st.rerun()

    report = state["report"]
    if report is None:
        return

    col1, col2, col3 = st.columns(3)
    col1.metric("通过", report.passed)
    col2.metric("失败", report.failed)
    col3.metric("通过率", f"{report.pass_rate:.0%}")
    st.markdown(report.summary_table())


def _section_evaluate(state: dict[str, Any]) -> None:
    st.header("4. 质量评测")

    col1, col2 = st.columns([1, 3])
    threshold = col1.slider("通过阈值", 50, 100, 80, step=5)
    use_judge = col2.checkbox("启用 LLM 评审 (需要 DEEPSEEK_API_KEY)", value=False)

    if st.button("重新评测", key="btn_score") and state["suite"] is not None:
        verdicts: dict[str, Any] = {}
        errors: dict[str, Any] = {}
        if use_judge:
            client = build_client(mode="auto")
            if client.mode != "real":
                st.info("未检测到 API Key, 已跳过 LLM 评审 (最终分 = 规则分)。")
            else:
                grouped: dict[str, list[Any]] = {}
                for case in state["suite"].cases:
                    grouped.setdefault(case.endpoint, []).append(case)
                with st.spinner("LLM 评审中 ..."):
                    for uid, record in run_judging(state["spec"], grouped, client).items():
                        if record.verdict is not None:
                            verdicts[uid] = record.verdict
                        else:
                            errors[uid] = record.error
        state["score"] = score_suite(
            state["spec"],
            state["suite"],
            threshold=float(threshold),
            verdicts=verdicts,
            judge_errors=errors,
        )
        st.rerun()

    score = state["score"]
    if score is None:
        return

    st.markdown(score.render())
    evaluation = state["evaluation"]
    if evaluation is not None:
        with st.expander("缺陷归因", expanded=False):
            st.markdown(evaluation.render())


def _section_export(state: dict[str, Any]) -> None:
    st.header("5. 导出")

    if state["suite"] is None:
        st.info("先生成或加载用例。")
        return

    if st.button("生成导出文件", type="primary", key="btn_export"):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = export_all(
                directory,
                spec=state["spec"],
                suite=state["suite"],
                report=state["report"],
                evaluation=state["evaluation"],
                score=state["score"],
                base_url_hint=state["base_url"] or "http://127.0.0.1:8000",
            )
            state["downloads"] = {
                name: (path.name, path.read_bytes()) for name, path in artifacts.items().items()
            }
        st.success("已生成, 点下方按钮下载。")

    downloads = state.get("downloads") or {}
    if not downloads:
        return
    for name, (filename, payload) in downloads.items():
        st.download_button(
            label=f"下载 {filename}",
            data=io.BytesIO(payload),
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if name == "excel"
            else "text/plain",
            key=f"dl_{name}",
        )


def main() -> None:
    if st is None:
        raise SystemExit("未安装 streamlit, 请先执行: pip install streamlit")
    st.set_page_config(page_title="接口用例智能生成与评测", layout="wide")
    st.title("接口用例智能生成与质量评测平台")
    st.caption(
        "上传 Swagger -> AI 生成用例 -> 字段白名单校验 -> 真实执行 -> 缺陷归因 -> 质量评分 -> 导出"
    )

    state = _state()
    _section_source(state)
    if state["spec"] is None:
        return
    _section_generate(state)
    _section_execute(state)
    _section_evaluate(state)
    _section_export(state)


if __name__ == "__main__":
    main()
