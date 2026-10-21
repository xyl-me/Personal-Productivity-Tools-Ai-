"""解析层基准: 不同规模下的耗时, 并做"有缓存 / 无缓存"的 A/B 对比。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from benchmarks.synthetic import build_document  # noqa: E402
from app.generator.client import endpoint_from_prompt  # noqa: E402
from app.generator.engine import run_generation  # noqa: E402
from app.generator.models import LLMResponse  # noqa: E402
from app.generator.prompts import build_user_prompt  # noqa: E402
from app.parser import parse_spec_dict  # noqa: E402
from app.parser.refs import RefResolver  # noqa: E402


class _BenchClient:
    """零延迟的假客户端: 只测调度/校验/去重这些平台自身开销, 不含模型延迟。"""

    mode = "bench"
    model = "bench"

    def __init__(self, index: dict) -> None:
        self._index = index

    async def complete(self, messages, *, temperature: float, max_tokens: int) -> LLMResponse:
        import json as _json
        import re as _re

        uid = endpoint_from_prompt(messages[-1]["content"]) or ""
        endpoint = self._index.get(uid)
        request: dict = {}
        if endpoint is not None:
            request["path_params"] = {name: "1" for name in _re.findall(r"\{(\w+)\}", endpoint.path)}
            if endpoint.request_body is not None:
                request["body"] = {}
        payload = {
            "cases": [
                {
                    "case_name": "bench-正向",
                    "category": "positive",
                    "type": "正向",
                    "request": request,
                    "expected": {"status": 200},
                }
            ]
        }
        return LLMResponse(text=_json.dumps(payload, ensure_ascii=False), model=self.model)


def measure_parse(document: dict, *, repeat: int, ref_cache: bool = True) -> tuple[float, int]:
    best = float("inf")
    count = 0
    for _ in range(repeat):
        start = time.perf_counter()
        spec = parse_spec_dict(document, ref_cache=ref_cache)
        best = min(best, time.perf_counter() - start)
        count = len(spec.endpoints)
    return best * 1000, count


def measure_resolution(document: dict, *, repeat: int, disable_cache: bool) -> tuple[float, int]:
    best = float("inf")
    hits = 0
    for _ in range(repeat):
        resolver = RefResolver(document, max_depth=512, disable_cache=disable_cache)
        start = time.perf_counter()
        resolver.resolve(document.get("paths") or {})
        best = min(best, time.perf_counter() - start)
        hits = resolver.cache_hits
    return best * 1000, hits


def measure_prompts(document: dict) -> tuple[float, int]:
    spec = parse_spec_dict(document)
    start = time.perf_counter()
    total_chars = sum(len(build_user_prompt(endpoint, count=6)) for endpoint in spec.endpoints)
    return (time.perf_counter() - start) * 1000, total_chars


def measure_generation(document: dict, *, concurrency: int = 8) -> tuple[float, int]:
    spec = parse_spec_dict(document)
    client = _BenchClient(spec.endpoint_index())
    start = time.perf_counter()
    run = run_generation(spec, client, max_concurrency=concurrency, cache=None)
    elapsed = (time.perf_counter() - start) * 1000
    return elapsed, run.total_cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析层基准")
    parser.add_argument("--sizes", nargs="*", type=int, default=[50, 100, 200, 400])
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args(argv)

    print("## A/B: $ref 缓存开 / 关\n")
    print("| 接口数 | 文档 | 整体解析 无缓存/有缓存(ms) | 提速 | $ref展开 无/有(ms) | 缓存命中 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for size in args.sizes:
        document = build_document(endpoints=size)
        payload = json.dumps(document, ensure_ascii=False)
        cached_ms, count = measure_parse(document, repeat=args.repeat, ref_cache=True)
        plain_ms, _ = measure_parse(document, repeat=args.repeat, ref_cache=False)
        raw_cached_ms, hits = measure_resolution(
            document, repeat=args.repeat, disable_cache=False
        )
        raw_plain_ms, _ = measure_resolution(document, repeat=args.repeat, disable_cache=True)

        speedup = plain_ms / cached_ms if cached_ms else 0.0
        print(
            f"| {count} | {len(payload) / 1024:.0f} KB | {plain_ms:.0f} / {cached_ms:.0f} | "
            f"{speedup:.1f}x | {raw_plain_ms:.0f} / {raw_cached_ms:.0f} | {hits} |"
        )

    document = build_document(endpoints=args.sizes[-1])
    print()
    print("说明: 第三列是**整体解析**耗时 (含提取与规范化), 第四列是它的提速;")
    print("      $ref 展开那一对数字更能说明缓存的直接作用。")

    prompt_ms, chars = measure_prompts(document)
    spec = parse_spec_dict(document)
    print()
    print(f"Prompt 构建: {prompt_ms:.0f} ms / {chars:,} 字符 "
          f"(平均 {chars // max(1, len(spec.endpoints)):,} 字符/接口)")

    gen_ms, cases = measure_generation(document)
    print(
        f"生成调度 (零延迟模型, 并发 8): {gen_ms:.0f} ms / {len(spec.endpoints)} 接口 / "
        f"{cases} 条用例 -> {len(spec.endpoints) / (gen_ms / 1000):.0f} 接口/秒"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
