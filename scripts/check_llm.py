"""LLM 连通性自检: 逐层验证 DNS -> TCP -> TLS -> HTTPS -> 鉴权 -> 模型调用。"""

from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import find_dotenv, load_dotenv  # noqa: E402

from app.generator.client import DEFAULT_BASE_URL, DEFAULT_MODEL, describe_error  # noqa: E402

OK = "✅"
FAIL = "❌"
WARN = "⚠️ "

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY")


def _host_of(base_url: str) -> str:
    stripped = base_url.split("://", 1)[-1]
    return stripped.split("/", 1)[0].split(":", 1)[0]


def _port_of(base_url: str) -> int:
    stripped = base_url.split("://", 1)[-1]
    host_part = stripped.split("/", 1)[0]
    if ":" in host_part:
        return int(host_part.rsplit(":", 1)[1])
    return 443 if base_url.startswith("https://") else 80


def step_env() -> dict[str, str]:
    print("== 1. 环境 ==")
    print(f"  Python {sys.version.split()[0]}  工作目录 {Path.cwd()}")

    dotenv_path = find_dotenv()
    print(f"  .env 自动定位: {dotenv_path or '(未找到)'}")
    load_dotenv()

    for name in PROXY_VARS:
        value = os.environ.get(name) or os.environ.get(name.upper()) or os.environ.get(name.lower())
        print(f"  {name:12s} = {value if value else '(未设置)'}")

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    shown = f"{key[:3]}***{key[-4:]} (长度 {len(key)})" if len(key) > 8 else ("(空)" if not key else "(过短)")
    print(f"  DEEPSEEK_API_KEY = {shown}")

    base_url = os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL
    print(f"  base_url = {base_url}   model = {model}")
    return {"key": key, "base_url": base_url, "model": model}


def step_dns(host: str) -> bool:
    print("\n== 2. DNS ==")
    try:
        start = time.perf_counter()
        infos = socket.getaddrinfo(host, None)
        ips = sorted({info[4][0] for info in infos})
        print(f"  {OK} {host} -> {', '.join(ips[:4])} ({(time.perf_counter()-start)*1000:.0f}ms)")
        return True
    except Exception as exc:
        print(f"  {FAIL} 解析失败: {describe_error(exc)}")
        print("     建议: 换 DNS (如 223.5.5.5) 或检查网络是否真的连着")
        return False


def step_tcp(host: str, port: int, timeout: float = 6.0) -> bool:
    print("\n== 3. TCP ==")
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            print(f"  {OK} {host}:{port} 建连成功 ({(time.perf_counter()-start)*1000:.0f}ms)")
            return True
    except Exception as exc:
        print(f"  {FAIL} {host}:{port} 建连失败: {describe_error(exc)}")
        print("     建议: 检查防火墙/代理; 若公司网需代理, 设置 HTTPS_PROXY 后重试")
        return False


def step_tls(host: str, port: int) -> bool:
    print("\n== 4. TLS ==")
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=6) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                print(f"  {OK} 握手成功 {tls.version()} / {tls.cipher()[0]}")
        return True
    except Exception as exc:
        print(f"  {FAIL} 握手失败: {describe_error(exc)}")
        print("     建议: 多为代理做了中间人但根证书没装, 或系统时间不对")
        return False


def step_https(base_url: str) -> bool:
    print("\n== 5. HTTPS 请求 ==")
    url = base_url.rstrip("/") + "/"
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            print(f"  {OK} HTTP {response.status} ({(time.perf_counter()-start)*1000:.0f}ms)")
            return True
    except urllib.error.HTTPError as exc:
        print(f"  {OK} HTTP {exc.code} —— 链路通 (无鉴权时报 401 属正常) "
              f"({(time.perf_counter()-start)*1000:.0f}ms)")
        return True
    except Exception as exc:
        print(f"  {FAIL} 请求失败: {describe_error(exc)}")
        return False


def step_model(key: str, base_url: str, model: str) -> bool:
    print("\n== 6. 模型调用 (1 token) ==")
    if not key:
        print(f"  {WARN} 没有 DEEPSEEK_API_KEY, 跳过")
        print("     把 Key 填到 .env 的 DEEPSEEK_API_KEY= 后面")
        return False
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=base_url, timeout=30, max_retries=0)
        start = time.perf_counter()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
        elapsed = (time.perf_counter() - start) * 1000
        usage = response.usage
        print(f"  {OK} 调用成功 ({elapsed:.0f}ms) 模型={response.model} "
              f"tokens={getattr(usage, 'total_tokens', '?')}")
        return True
    except Exception as exc:
        print(f"  {FAIL} 调用失败: {describe_error(exc)}")
        text = describe_error(exc).lower()
        if "401" in text or "authentication" in text or "invalid" in text:
            print("     建议: Key 无效/过期。去 platform.deepseek.com 确认, 注意别带引号和空格")
        elif "402" in text or "balance" in text or "quota" in text:
            print("     建议: 账户余额不足, 充值后再试")
        elif "404" in text or "model" in text:
            print(f"     建议: 模型名不对。当前用的是 {model!r}, 可试 deepseek-chat")
        elif "429" in text:
            print("     建议: 被限流, 稍后再试或降低并发")
        else:
            print("     建议: 网络类问题, 见上面几步的结论")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 连通性自检")
    parser.add_argument("--no-call", action="store_true", help="跳过模型调用")
    args = parser.parse_args(argv)

    env = step_env()
    host, port = _host_of(env["base_url"]), _port_of(env["base_url"])

    checks = [step_dns(host)]
    if checks[-1]:
        checks.append(step_tcp(host, port))
    if all(checks):
        checks.append(step_tls(host, port))
        checks.append(step_https(env["base_url"]))
    if all(checks) and not args.no_call:
        checks.append(step_model(env["key"], env["base_url"], env["model"]))

    print()
    if all(checks):
        print("结论: 全部通过, 可以跑 python scripts/run_m2.py --real")
        return 0
    print("结论: 有检查项未通过, 按上面每步的建议处理; 也可以先跑 --mock 继续演示")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
