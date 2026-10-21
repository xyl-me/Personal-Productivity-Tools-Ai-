"""生成层的契约模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CaseCategory(str, Enum):
    """用例的设计方法来源。M3 的规则评分要靠它检查"维度是否覆盖"。"""

    POSITIVE = "positive"
    BOUNDARY = "boundary"
    EQUIVALENCE = "equivalence"
    ERROR_GUESS = "error_guess"
    SECURITY = "security"
    OTHER = "other"

    @classmethod
    def parse(cls, value: Any) -> "CaseCategory":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        mapping = {
            "正向": cls.POSITIVE,
            "正常": cls.POSITIVE,
            "positive": cls.POSITIVE,
            "happy": cls.POSITIVE,
            "边界": cls.BOUNDARY,
            "边界值": cls.BOUNDARY,
            "boundary": cls.BOUNDARY,
            "等价类": cls.EQUIVALENCE,
            "异常": cls.EQUIVALENCE,
            "equivalence": cls.EQUIVALENCE,
            "invalid": cls.EQUIVALENCE,
            "错误推测": cls.ERROR_GUESS,
            "error_guess": cls.ERROR_GUESS,
            "guess": cls.ERROR_GUESS,
            "安全": cls.SECURITY,
            "越权": cls.SECURITY,
            "security": cls.SECURITY,
        }
        for key, category in mapping.items():
            if key in text:
                return category
        return cls.OTHER


class ExpectedSpec(BaseModel):
    """模型对"正确行为"的判断。"""

    model_config = ConfigDict(extra="ignore")

    status: int | list[int] | None = None
    note: str | None = None

    @property
    def statuses(self) -> list[int]:
        if self.status is None:
            return []
        return [self.status] if isinstance(self.status, int) else list(self.status)


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


class GeneratedCase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_name: str
    category: str = "other"
    type: str | None = None
    request: RequestSpec = Field(default_factory=RequestSpec)
    expected: ExpectedSpec = Field(default_factory=ExpectedSpec)
    scenario: str | None = None
    rationale: str | None = None

    @field_validator("case_name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = (value or "").strip()
        return text or "未命名用例"

    @property
    def normalized_category(self) -> CaseCategory:
        return CaseCategory.parse(self.category or self.type)


class GeneratedBatch(BaseModel):
    """一次调用的完整返回。``cases`` 之外的内容一律忽略。"""

    model_config = ConfigDict(extra="ignore")

    cases: list[GeneratedCase] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cases)


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class EndpointGeneration(BaseModel):
    """单个接口的生成过程与结果。保留失败细节, 便于排查是哪一步被拦下来的。"""

    model_config = ConfigDict(extra="ignore")

    endpoint: str
    accepted: list[Any] = Field(default_factory=list)
    attempts: int = 0
    parse_errors: list[str] = Field(default_factory=list)
    dropped_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    duration_ms: float = 0.0

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)


class GenerationRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = ""
    mode: str = "real"
    started_at: str = ""
    duration_ms: float = 0.0
    endpoints: list[EndpointGeneration] = Field(default_factory=list)

    @property
    def total_cases(self) -> int:
        return sum(ep.accepted_count for ep in self.endpoints)

    @property
    def dropped_fields(self) -> list[str]:
        return [field for ep in self.endpoints for field in ep.dropped_fields]

    @property
    def attempts(self) -> int:
        return sum(ep.attempts for ep in self.endpoints)

    @property
    def retries(self) -> int:
        """排除首次尝试后的重试次数。"""
        return sum(max(0, ep.attempts - 1) for ep in self.endpoints)

    @property
    def cache_hits(self) -> int:
        return sum(1 for ep in self.endpoints if ep.cached)

    @property
    def prompt_tokens(self) -> int:
        return sum(ep.prompt_tokens for ep in self.endpoints)

    @property
    def completion_tokens(self) -> int:
        return sum(ep.completion_tokens for ep in self.endpoints)

    @property
    def parse_error_count(self) -> int:
        return sum(len(ep.parse_errors) for ep in self.endpoints)

    def summary(self) -> str:
        lines = [
            f"模型 {self.model} ({self.mode}) / 接口 {len(self.endpoints)} 个 / "
            f"产出用例 {self.total_cases} 条 / 耗时 {self.duration_ms:.0f}ms",
            f"调用 {self.attempts} 次 (重试 {self.retries} 次, 缓存命中 {self.cache_hits} 次) / "
            f"解析失败 {self.parse_error_count} 次",
            f"Token: prompt {self.prompt_tokens} + completion {self.completion_tokens}",
            f"幻觉字段被拦截: {len(self.dropped_fields)} 个"
            + (f" -> {', '.join(sorted(set(self.dropped_fields)))}" if self.dropped_fields else ""),
        ]
        return "\n".join(lines)
