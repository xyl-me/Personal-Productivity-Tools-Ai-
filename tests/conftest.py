"""pytest 全局配置。"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

_TMP_ROOT = Path(__file__).parent / "_tmp"


def _bypass_proxy_for_loopback() -> None:
    """测试全部打本机, 必须绕过系统代理。

    开了 VPN 之后 Windows 会设置 ProxyServer, httpx / urllib 会读它,
    把 http://127.0.0.1:xxxx 也送去代理, 于是所有请求变成 502。
    """
    hosts = ["127.0.0.1", "localhost", "::1"]
    current = os.environ.get("NO_PROXY", "")
    merged = [item for item in current.split(",") if item.strip()]
    for host in hosts:
        if host not in merged:
            merged.append(host)
    os.environ["NO_PROXY"] = ",".join(merged)
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


_bypass_proxy_for_loopback()


@pytest.fixture()
def tmp_dir() -> Iterator[Path]:
    path = _TMP_ROOT / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
