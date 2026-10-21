"""大模型客户端。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import LLMResponse

_ENDPOINT_RE = re.compile(r"^## ([A-Z]+) (\S+)$", re.MULTILINE)

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"


@runtime_checkable
class LLMClient(Protocol):
    """生成层只依赖这一个方法。"""

    model: str
    mode: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse: ...


def endpoint_from_prompt(prompt: str) -> str | None:
    """从 Prompt 里认出目标接口 (渲染的 markdown 第一行固定是 ``## METHOD /path``)。"""
    match = _ENDPOINT_RE.search(prompt)
    return f"{match.group(1)} {match.group(2)}" if match else None


class OutputTruncated(RuntimeError):
    """模型输出被 ``max_tokens`` 截断 (``finish_reason == "length"``)。"""

    truncated = True
    retryable = False

    def __init__(self, message: str, *, completion_tokens: int = 0) -> None:
        super().__init__(message)
        self.completion_tokens = completion_tokens


class DeepSeekClient:
    """真实客户端。延迟导入 openai, 保证没装依赖时 mock 模式仍可运行。"""

    mode = "real"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 90.0,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 DeepSeek API Key")
        from openai import AsyncOpenAI

        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        started = time.perf_counter()
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = response.usage
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0

        if getattr(choice, "finish_reason", None) == "length":
            raise OutputTruncated(
                f"输出被截断: 已达 max_tokens={max_tokens}, 生成 {completion_tokens} tokens / "
                f"{len(text)} 字符",
                completion_tokens=completion_tokens,
            )

        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class MockFixtureMissing(RuntimeError):
    """fixtures 里没有这个接口的录制响应。重试也不会变好, 所以标记为不可重试。"""

    retryable = False


def describe_error(exc: BaseException) -> str:
    """把异常连同它的**根因链**渲染成一行。"""
    parts: list[str] = [f"{type(exc).__name__}: {exc}".strip()]
    seen: set[int] = set()
    cause = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen and len(parts) < 5:
        seen.add(id(cause))
        text = f"{type(cause).__name__}: {cause}".strip()
        if text not in parts:
            parts.append(text)
        cause = cause.__cause__ or cause.__context__
    return " <- ".join(parts)


def describe_connection_hint(exc: BaseException) -> str | None:
    """针对常见的连接类失败给一句可执行的建议。"""
    text = " ".join(
        f"{type(node).__name__} {node}"
        for node in _exception_chain(exc)
    ).lower()
    if "certificate" in text or "ssl" in text:
        return "TLS/证书问题: 可能是公司代理做了中间人, 或系统根证书缺失"
    if "proxy" in text:
        return "代理配置有问题: 检查 HTTP_PROXY / HTTPS_PROXY 是否指向了已停止的代理"
    if "timeout" in text or "timed out" in text:
        return "连接超时: 检查网络/代理/DNS, 或换一个可达的中转地址"
    if "connect" in text or "connection" in text:
        return "连不上: 检查代理变量、防火墙、是否需要开全局代理"
    if "401" in text or "authentication" in text or "invalid" in text:
        return "鉴权失败: Key 无效、过期, 或复制时带了多余空格/引号"
    if "429" in text or "insufficient" in text or "balance" in text:
        return "被限流或余额不足"
    return None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = [exc]
    seen: set[int] = {id(exc)}
    cause = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen and len(chain) < 5:
        chain.append(cause)
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return chain


class MockClient:
    """离线回放客户端。"""

    mode = "mock"

    def __init__(
        self,
        fixtures: dict[str, Any],
        *,
        model: str = "mock-deepseek",
    ) -> None:
        self.model = model
        self._fixtures = fixtures

    @classmethod
    def from_file(cls, path: str | Path, *, model: str = "mock-deepseek") -> "MockClient":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        fixtures = data.get("responses", data) if isinstance(data, dict) else {}
        return cls(fixtures, model=model)

    @property
    def endpoints(self) -> list[str]:
        return sorted(self._fixtures)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        prompt = messages[-1]["content"] if messages else ""
        endpoint = endpoint_from_prompt(prompt)
        if endpoint is None or endpoint not in self._fixtures:
            raise MockFixtureMissing(f"fixtures 中没有接口 {endpoint!r} 的录制响应")

        payload = self._fixtures[endpoint]
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=max(1, len(prompt) // 3),
            completion_tokens=max(1, len(text) // 3),
        )


def prompt_key(model: str, temperature: float, prompt: str) -> str:
    digest = hashlib.sha256(f"{model}|{temperature:.4f}|{prompt}".encode("utf-8"))
    return digest.hexdigest()


def build_client(
    *,
    mode: str = "auto",
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    fixtures_path: str | Path | None = None,
) -> LLMClient:
    """工厂: ``auto`` 有 Key 走真实调用, 没有则回放 fixtures。"""
    resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    if mode == "auto":
        mode = "real" if resolved_key else "mock"

    if mode == "real":
        if not resolved_key:
            raise RuntimeError(
                "没有找到 DEEPSEEK_API_KEY。请在 .env 中配置, 或显式使用 --mock 离线模式。"
            )
        return DeepSeekClient(resolved_key, model=model, base_url=base_url)

    if mode == "mock":
        if fixtures_path is None:
            fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "mock_llm" / "responses.json"
        return MockClient.from_file(fixtures_path)

    raise ValueError(f"未知模式: {mode!r} (可选 real / mock / auto)")
