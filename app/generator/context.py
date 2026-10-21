"""测试运行时上下文: 凭据、已知存在的资源 ID。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("data") / "runtime.json"


@dataclass
class RuntimeContext:
    """运行时注入的凭据与已知数据。"""

    headers: dict[str, str] = field(default_factory=dict)
    path_params: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = DEFAULT_PATH) -> "RuntimeContext":
        if path is None:
            return cls()
        file_path = Path(path)
        if not file_path.is_file():
            return cls()
        data: dict[str, Any] = json.loads(file_path.read_text(encoding="utf-8"))
        return cls(
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            path_params={str(k): str(v) for k, v in (data.get("path_params") or {}).items()},
            query={str(k): str(v) for k, v in (data.get("query") or {}).items()},
        )

    @property
    def empty(self) -> bool:
        return not (self.headers or self.path_params or self.query)

    def prompt_section(self) -> str:
        """给 Prompt 用的说明 —— **只有名字, 没有值**。"""
        if self.empty:
            return ""
        lines = ["## 运行时上下文 (平台会注入, 你不需要编造具体值)"]
        if self.headers:
            lines.append(
                "- 以下 header 存在有效凭据, 需要鉴权成功的用例只需在 request.headers 里写出这些名字, "
                "值留空字符串即可: " + ", ".join(sorted(self.headers))
            )
        if self.path_params:
            lines.append(
                "- 以下路径参数有已知存在的资源 ID, 需要『资源存在』的用例直接使用即可: "
                + ", ".join(sorted(self.path_params))
            )
        if self.query:
            lines.append("- 以下查询参数有可用取值: " + ", ".join(sorted(self.query)))
        return "\n".join(lines) + "\n"


def is_success_expectation(statuses: list[int]) -> bool:
    return bool(statuses) and all(code < 400 for code in statuses)


def needs_credentials(statuses: list[int]) -> bool:
    """这条用例需不需要带凭据。"""
    if not statuses:
        return True
    return not any(code in (401, 403) for code in statuses)


def needs_existing_resource(statuses: list[int]) -> bool:
    """这条用例是不是要求"资源真实存在"。"""
    return bool(statuses) and all(200 <= code < 300 for code in statuses)
