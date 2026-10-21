"""LLM-as-a-Judge: 只评规则判定不了的部分。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.common.jsonutil import JsonParseError, format_validation_error, loads_tolerant
from app.executor.models import TestCase
from app.parser.models import EndpointSpec
from app.parser.render import render_endpoint_markdown
from app.generator.models import LLMResponse

JUDGE_SYSTEM_PROMPT = """你是一名资深测试架构师, 负责评审一组自动生成的接口测试用例。

你**不需要**评估这些维度 (已由程序精确判定, 你不要重复打分):
- 有没有必填缺失用例、长度/数值边界用例、类型错误用例、枚举外值用例
- 有没有鉴权/越权断言、有没有注入类输入
- 用例是否重复

请只评估程序无法判定的部分:
1. **业务语义合理性**: 这些用例是否真的在测这个接口的业务规则?
2. **场景组合价值**: 有没有明显该测但没测的业务场景?
3. **预期结果的正确性**: expected.status 是否符合业务常识?
4. **用例可读性**: 名称与描述能否让执行者看懂意图?

评分要求:
- score 为 0-100 的整数, 不要集中在 80 附近; 有明显缺失就给 60 以下
- 每条 issue 必须给出 case_id (能定位到具体用例); 无法定位的意见不要提
- severity 取 high / medium / low, 只有"会导致漏测关键缺陷"才用 high
- 输出严格 JSON, 不要代码块标记, 不要解释性文字
"""

JUDGE_OUTPUT_TEMPLATE = {
    "score": 72,
    "strengths": ["覆盖了分页与金额的下边界"],
    "issues": [
        {"case_id": "AI-xxx-003", "problem": "该用例断言 200, 但文档未声明该字段可省略", "severity": "medium"}
    ],
    "dimensions": {"business_semantics": 70, "scenario_value": 65, "expected_correctness": 80, "readability": 75},
    "note": "整体可用, 但缺少订单状态流转的跨接口场景",
}


class JudgeIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    problem: str
    severity: str = "medium"
    case_id: str | None = None


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    score: int
    strengths: list[str] = Field(default_factory=list)
    issues: list[JudgeIssue] = Field(default_factory=list)
    dimensions: dict[str, int] = Field(default_factory=dict)
    note: str | None = None

    @field_validator("score", mode="before")
    @classmethod
    def _clamp(cls, value: Any) -> int:
        try:
            number = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"score 必须是数字, 实际是 {value!r}") from exc
        return max(0, min(100, number))

    @property
    def high_issues(self) -> list[JudgeIssue]:
        return [issue for issue in self.issues if issue.severity.lower() == "high"]


@dataclass
class JudgeRecord:
    endpoint: str
    verdict: JudgeVerdict | None = None
    error: str | None = None
    raw: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False


def parse_verdict(text: str) -> JudgeVerdict:
    data = loads_tolerant(text)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise JsonParseError("评审结果顶层必须是 JSON 对象")
    try:
        return JudgeVerdict.model_validate(data)
    except ValidationError as exc:
        raise JsonParseError(f"评审结果结构不合法: {format_validation_error(exc)}") from exc


def render_cases(cases: Sequence[TestCase], *, limit: int = 24) -> str:
    lines = ["| 用例 | 名称 | 请求 | 期望 | 覆盖场景 |", "| --- | --- | --- | --- | --- |"]
    for case in list(cases)[:limit]:
        request = f"{case.method} {case.path}"
        if case.query:
            request += "?" + "&".join(f"{k}={v}" for k, v in case.query.items())
        if case.headers:
            request += " [headers:" + ",".join(case.headers) + "]"
        if case.body is not None:
            request += f" body={case.body}"
        expected = "/".join(str(code) for code in case.expect.statuses)
        scenario = (case.description or "").replace("|", "\\|")
        lines.append(
            f"| {case.id} | {case.name} | `{request}` | {expected} | {scenario} |"
        )
    if len(cases) > limit:
        lines.append(f"| ... | 其余 {len(cases) - limit} 条已省略 | | | |")
    return "\n".join(lines)


def build_judge_prompt(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> str:
    import json

    template = json.dumps(JUDGE_OUTPUT_TEMPLATE, ensure_ascii=False, indent=2)
    return f"""## 接口定义

{render_endpoint_markdown(endpoint)}

## 待评审用例 (共 {len(cases)} 条)

{render_cases(cases)}

## 输出格式 (严格 JSON, 不要代码块标记)

{template}
"""


async def judge_endpoint(
    endpoint: EndpointSpec,
    cases: Sequence[TestCase],
    client: Any,
    *,
    temperature: float = 0.2,
    max_tokens: int = 1500,
) -> JudgeRecord:
    """评审一个接口的用例集。失败不抛异常, 在 ``record.error`` 里说明原因。"""
    record = JudgeRecord(endpoint=endpoint.uid)
    if not cases:
        record.error = "该接口没有用例, 跳过评审"
        return record

    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": build_judge_prompt(endpoint, cases)},
    ]
    try:
        response: LLMResponse = await client.complete(
            messages, temperature=temperature, max_tokens=max_tokens
        )
    except Exception as exc:  # noqa: BLE001 - 评审失败不该中断整轮评测
        record.error = f"调用失败: {type(exc).__name__}: {exc}"
        return record

    record.raw = response.text
    record.prompt_tokens = response.prompt_tokens
    record.completion_tokens = response.completion_tokens
    record.cached = response.cached
    try:
        record.verdict = parse_verdict(response.text)
    except JsonParseError as exc:
        record.error = str(exc)
    return record


async def judge_suite(
    spec: Any,
    grouped: dict[str, list[TestCase]],
    client: Any,
    *,
    endpoints: Sequence[str] | None = None,
    max_concurrency: int = 3,
    **kwargs: Any,
) -> dict[str, JudgeRecord]:
    index = spec.endpoint_index()
    targets = [
        uid for uid in (endpoints or grouped) if uid in index and grouped.get(uid)
    ]
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def worker(uid: str) -> JudgeRecord:
        async with semaphore:
            return await judge_endpoint(index[uid], grouped[uid], client, **kwargs)

    records = await asyncio.gather(*(worker(uid) for uid in targets))
    return {record.endpoint: record for record in records}


def run_judging(spec: Any, grouped: dict[str, list[TestCase]], client: Any, **kwargs: Any) -> dict[str, JudgeRecord]:
    return asyncio.run(judge_suite(spec, grouped, client, **kwargs))
