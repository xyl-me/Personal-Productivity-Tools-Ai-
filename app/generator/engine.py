"""生成引擎: 并发调度 + 解析重试 + 缓存 + 校验。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from pydantic import ValidationError

from app.common.jsonutil import JsonParseError, format_validation_error, loads_tolerant

from .cache import GenerationCache
from .client import (
    LLMClient,
    OutputTruncated,
    describe_connection_hint,
    describe_error,
    prompt_key,
)
from .context import RuntimeContext
from .models import EndpointGeneration, GeneratedBatch, GenerationRun, LLMResponse
from .prompts import (
    build_messages,
    build_retry_prompt,
    build_truncation_prompt,
    build_user_prompt,
)
from .validate import convert_case

_SLUG_RE = re.compile(r"[^A-Za-z0-9\-]+")

ParseError = JsonParseError


def parse_batch(text: str) -> GeneratedBatch:
    data = loads_tolerant(text)
    if isinstance(data, list):
        data = {"cases": data}
    if not isinstance(data, dict):
        raise ParseError("顶层必须是 JSON 对象")
    if "cases" not in data:
        if "case_name" in data:
            data = {"cases": [data]}
        else:
            raise ParseError("缺少 cases 字段")

    try:
        batch = GeneratedBatch.model_validate(data)
    except ValidationError as exc:
        raise ParseError(f"结构校验失败: {format_validation_error(exc)}") from exc
    if not batch.cases:
        raise ParseError("cases 为空数组")
    return batch


_PERMANENT_HINTS = ("401", "403", "invalid api key", "unauthorized", "authentication", "invalid_api_key")


def _is_retryable(exc: Exception) -> bool:
    if getattr(exc, "retryable", True) is False:
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    return not any(hint in text for hint in _PERMANENT_HINTS)


async def _complete_with_backoff(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> LLMResponse:
    """传输层重试: 429 / 5xx / 超时 / 连接断开才重试, 认证错误立即失败。"""
    last: Exception | None = None
    for index in range(attempts):
        try:
            return await client.complete(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - 需要按类型区分是否重试
            last = exc
            if not _is_retryable(exc) or index == attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2**index))
    raise last  # pragma: no cover - 循环必然 return 或 raise


def _case_id_prefix(endpoint_uid: str) -> str:
    slug = _SLUG_RE.sub("-", endpoint_uid).strip("-")
    return f"AI-{slug}"


def _case_signature(case: Any) -> str:
    return json.dumps(
        [case.path, case.query, case.body, case.expect.status],
        sort_keys=True,
        default=str,
    )


async def generate_endpoint(
    endpoint: Any,
    client: LLMClient,
    *,
    count: int = 6,
    temperature: float = 0.1,
    max_tokens: int = 8192,
    max_retries: int = 2,
    cache: GenerationCache | None = None,
    case_id_prefix: str | None = None,
    context: RuntimeContext | None = None,
) -> EndpointGeneration:
    started = time.perf_counter()
    record = EndpointGeneration(endpoint=endpoint.uid)
    base_prompt = build_user_prompt(endpoint, count=count, context=context)
    prompt = base_prompt
    seen: set[str] = set()
    prefix = case_id_prefix or _case_id_prefix(endpoint.uid)
    current_count = count

    for attempt in range(1, max_retries + 2):
        record.attempts = attempt
        key = prompt_key(client.model, temperature, prompt)

        cached_text = cache.get(key) if cache is not None else None
        if cached_text is not None:
            response = LLMResponse(text=cached_text, model=client.model, cached=True)
            record.cached = True
        else:
            try:
                response = await _complete_with_backoff(
                    client,
                    build_messages(prompt),
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except OutputTruncated as exc:
                record.completion_tokens += exc.completion_tokens
                record.parse_errors.append(f"输出截断: {exc}")
                if attempt > max_retries or current_count <= 1:
                    break
                current_count = max(1, current_count // 2)
                prompt = build_truncation_prompt(
                    endpoint,
                    count=current_count,
                    tokens=exc.completion_tokens,
                    context=context,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 调用失败要记录而不是炸掉整轮
                message = f"调用失败: {describe_error(exc)}"
                hint = describe_connection_hint(exc)
                if hint:
                    message = f"{message} [{hint}]"
                record.parse_errors.append(message)
                break

        record.prompt_tokens += response.prompt_tokens
        record.completion_tokens += response.completion_tokens

        try:
            batch = parse_batch(response.text)
        except ParseError as exc:
            record.parse_errors.append(str(exc))
            if attempt > max_retries:
                break
            prompt = build_retry_prompt(
                base_prompt, error=str(exc), bad_output=response.text, count=count
            )
            continue

        if cache is not None and not response.cached:
            cache.put(key, response.text, client.model)

        for generated in batch.cases:
            conversion = convert_case(
                generated,
                endpoint,
                index=len(record.accepted) + 1,
                case_id_prefix=prefix,
                context=context,
            )
            record.dropped_fields.extend(conversion.dropped_fields)
            record.warnings.extend(conversion.warnings)
            if conversion.case is None:
                record.rejected.append(" | ".join(conversion.errors) or "未知原因")
                continue

            signature = _case_signature(conversion.case)
            if signature in seen:
                record.rejected.append(f"重复用例: {conversion.case.name}")
                continue
            seen.add(signature)
            record.accepted.append(conversion.case)
        break

    record.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    return record


async def generate_all(
    spec: Any,
    client: LLMClient,
    *,
    endpoints: Iterable[str] | None = None,
    count: int = 6,
    temperature: float = 0.1,
    max_tokens: int = 8192,
    max_retries: int = 2,
    max_concurrency: int = 4,
    cache: GenerationCache | None = None,
    context: RuntimeContext | None = None,
) -> GenerationRun:
    """并发跑完一组接口。并发只用于**无共享状态**的 API 调用, 与用例执行不同。"""
    wanted: set[str] | None = set(endpoints) if endpoints else None
    targets: Sequence[Any] = [
        endpoint for endpoint in spec.endpoints if wanted is None or endpoint.uid in wanted
    ]

    started = time.perf_counter()
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def worker(endpoint: Any) -> EndpointGeneration:
        async with semaphore:
            try:
                return await generate_endpoint(
                    endpoint,
                    client,
                    count=count,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=max_retries,
                    cache=cache,
                    context=context,
                )
            except Exception as exc:  # noqa: BLE001 - 单个接口失败不影响其它接口
                return EndpointGeneration(
                    endpoint=endpoint.uid,
                    parse_errors=[f"未捕获异常: {type(exc).__name__}: {exc}"],
                )

    results = await asyncio.gather(*(worker(endpoint) for endpoint in targets))
    return GenerationRun(
        model=client.model,
        mode=getattr(client, "mode", "real"),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        endpoints=list(results),
    )


def run_generation(
    spec: Any,
    client: LLMClient,
    **kwargs: Any,
) -> GenerationRun:
    """同步入口, 给脚本和 Streamlit 用。"""
    return asyncio.run(generate_all(spec, client, **kwargs))


def to_suite(run: GenerationRun, *, name: str = "ai_generated") -> Any:
    """把整轮生成结果收成用例集 (与手写用例完全同构)。"""
    from app.executor.models import TestCaseSuite

    cases = [case for endpoint in run.endpoints for case in endpoint.accepted]
    return TestCaseSuite(suite=name, target=run.model, cases=cases)
