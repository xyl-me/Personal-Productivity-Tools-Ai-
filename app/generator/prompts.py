"""Prompt 构造。"""

from __future__ import annotations

import json
from typing import Any

from app.parser.models import EndpointSpec
from app.parser.render import render_endpoint_markdown

from .context import RuntimeContext

SYSTEM_PROMPT = """你是一名有 10 年经验的资深接口测试专家, 精通等价类划分、边界值分析、错误推测与接口安全测试。

你的输出必须是**严格的 JSON 对象**, 不能包含 markdown 代码块标记, 不能有任何解释性文字。

硬性约束 (违反会导致用例作废):
1. 只能使用下方"接口定义"里明确列出的参数名和 body 字段名, 严禁发明字段。
2. request.path_params 必须覆盖 URL 路径中的全部占位符。
3. expected.status 填**按接口规范/业务常识应当返回**的状态码, 不是你猜测服务当前会返回什么。
4. 正向用例至少 1 条; 其余用例应覆盖边界值、等价类异常、必填缺失、类型错误、越权/鉴权缺失。
5. 用例之间不要重复: 同一场景只需一条。
6. **请求头参数必须显式给出**: 如果接口声明了 X-Admin-Token 这类凭据类 header, 至少要有一条
   "带正确凭据"的用例 (在 request.headers 里写出来), 以及一条"不带凭据"的用例;
   忽略 header 会导致期望 200 却返回 401, 用例直接作废。
"""

RETRY_TEMPLATE = """你上一次的输出无法被解析为合法 JSON。

错误信息:
{error}

上一次输出的片段:
{preview}

请严格按照要求的 JSON 结构重新输出 {count} 条用例, 只输出 JSON 本体, 不要包含 ```json 标记。
"""

OUTPUT_TEMPLATE: dict[str, Any] = {
    "cases": [
        {
            "case_name": "创建订单-金额为负数",
            "category": "boundary",
            "type": "异常",
            "scenario": "边界值: 金额下边界之外 (负数)",
            "request": {
                "path_params": {},
                "query": {},
                "headers": {},
                "body": {"amount": -1},
            },
            "expected": {"status": 400, "note": "金额必须大于 0"},
        }
    ]
}


def _legal_param_lines(endpoint: EndpointSpec) -> list[str]:
    lines: list[str] = []
    grouped: dict[str, list[str]] = {}
    for param in endpoint.params:
        kind = param.type or "any"
        if param.is_array and param.item_type:
            kind = f"array<{param.item_type}>"
        flag = ", 必填" if param.required else ""
        grouped.setdefault(param.location.value, []).append(f"{param.name} ({kind}{flag})")

    labels = {
        "path": "路径参数",
        "query": "查询参数",
        "header": "请求头",
        "cookie": "Cookie",
        "formData": "表单字段",
    }
    for location, items in grouped.items():
        lines.append(f"- {labels.get(location, location)}: " + ", ".join(items))

    if endpoint.request_body is not None:
        paths = sorted(endpoint.body_property_paths())
        if paths:
            required = endpoint.request_body.json_schema.get("required") or []
            text = ", ".join(f"{p}{' (必填)' if p in required else ''}" for p in paths)
            lines.append(f"- body 字段: {text}")
        else:
            lines.append("- body 字段: 自由结构 (文档未声明固定字段)")

    if not lines:
        lines.append("- 该接口无请求参数")
    return lines


def build_user_prompt(
    endpoint: EndpointSpec,
    *,
    count: int,
    context: "RuntimeContext | None" = None,
) -> str:
    """构造单个接口的生成请求。"""
    schema = json.dumps(OUTPUT_TEMPLATE, ensure_ascii=False, indent=2)
    context_section = context.prompt_section() if context is not None else ""
    return f"""## 接口定义

{render_endpoint_markdown(endpoint)}

## 合法字段清单 (只能使用这些)

{chr(10).join(_legal_param_lines(endpoint))}

{context_section}## 任务

为上面的接口生成 {count} 条测试用例, 覆盖以下维度 (不适用时跳过, 但要说明为什么不适用):

1. **正向**: 最小合法请求、完整合法请求
2. **边界值**: 字符串长度、数值、分页、金额等上下边界及越界值
3. **等价类异常**: 类型错误、必填缺失、枚举外值、格式非法
4. **错误推测 / 安全**: 注入串、超长字符串、越权访问、非法状态流转

category 只能取: positive / boundary / equivalence / error_guess / security
type 只能取: 正向 / 异常

## 输出格式 (严格 JSON, 不要代码块标记)

{schema}

现在输出 {count} 条用例的 JSON。
"""


def build_retry_prompt(original: str, *, error: str, bad_output: str, count: int) -> str:
    """把上一次的失败原因反馈回去。比"重试一次同样的 prompt"有效得多。"""
    preview = (bad_output or "")[:400]
    return (
        original
        + "\n\n"
        + RETRY_TEMPLATE.format(error=error, preview=preview, count=count)
    )


TRUNCATION_TEMPLATE = """## 重要: 上一次请求的输出被截断了

上一次的 JSON 没有闭合就被 max_tokens 截断（已生成 {tokens} tokens）。
这次请只输出 **{count} 条**用例, 并且:
- scenario 与 note 各控制在 20 字以内
- 不要输出任何解释性文字、不要用 ```json 包裹
- 必须保证 JSON 完整闭合

现在输出 {count} 条用例。
"""


def build_truncation_prompt(
    endpoint: EndpointSpec,
    *,
    count: int,
    tokens: int = 0,
    context: "RuntimeContext | None" = None,
) -> str:
    """截断后的重试 prompt: 条数减半 + 要求精简描述。"""
    base = build_user_prompt(endpoint, count=count, context=context)
    return base + "\n\n" + TRUNCATION_TEMPLATE.format(count=count, tokens=tokens)


def build_messages(user_prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
