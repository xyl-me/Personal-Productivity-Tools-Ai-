"""在后台线程里跑一个**真实** HTTP 服务。"""

from __future__ import annotations

import copy
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator


def free_port(host: str = "127.0.0.1") -> int:
    """让内核分配一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _quiet_logging_config() -> dict[str, Any]:
    """把 uvicorn 的日志级别压到 CRITICAL。"""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    loggers = config.get("loggers") or {}
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        if name in loggers:
            loggers[name]["level"] = "CRITICAL"
    return config


@contextmanager
def live_server(
    app: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    ready_timeout: float = 15.0,
    shutdown_timeout: float = 5.0,
    quiet: bool = True,
) -> Iterator[str]:
    """启动服务并 yield ``base_url``; 退出时关闭。"""
    import uvicorn

    actual_port = port or free_port(host)
    config = uvicorn.Config(
        app,
        host=host,
        port=actual_port,
        log_level="critical" if quiet else "info",
        access_log=not quiet,
        lifespan="on",
        log_config=_quiet_logging_config() if quiet else uvicorn.config.LOGGING_CONFIG,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="live-server", daemon=True)
    thread.start()

    try:
        deadline = time.monotonic() + ready_timeout
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("服务线程在启动阶段退出 (端口被占用?)")
            if time.monotonic() > deadline:
                raise RuntimeError(f"服务在 {ready_timeout}s 内未就绪")
            time.sleep(0.02)
        yield f"http://{host}:{actual_port}"
    finally:
        server.should_exit = True
        thread.join(timeout=shutdown_timeout)
