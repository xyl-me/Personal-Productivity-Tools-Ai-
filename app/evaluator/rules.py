"""规则子分: 用代码判断"这套用例该测的维度测到了没有"。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import unquote

from app.parser.models import EndpointSpec, ParamLocation

from app.executor.models import TestCase

INJECTION_MARKERS = (
    "'",
    '"',
    "--",
    ";",
    "1=1",
    "or 1",
    "<script",
    "%00",
    "../",
    "drop table",
)

_NUMERIC_TYPES = {"integer": int, "number": (int, float)}
_STRING_MARKER = "string"


@dataclass(frozen=True)
class FieldSpec:
    """一个可被用例赋值的字段 (query/header/cookie 参数, 或 body 里的某个路径)。"""

    location: str
    path: str
    schema: dict[str, Any]

    @property
    def kind(self) -> str | None:
        value = self.schema.get("type")
        return value if isinstance(value, str) else None

    @property
    def enum(self) -> list[Any]:
        value = self.schema.get("enum")
        return list(value) if isinstance(value, list) else []

    @property
    def required(self) -> bool:
        return bool(self.schema.get("required"))


def _walk_properties(schema: Any, prefix: str = "", depth: int = 0) -> Iterable[tuple[str, dict]]:
    if not isinstance(schema, dict) or depth > 8:
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for name, sub in properties.items():
        if not isinstance(sub, dict):
            continue
        path = f"{prefix}.{name}" if prefix else str(name)
        yield path, sub
        yield from _walk_properties(sub, path, depth + 1)
        items = sub.get("items")
        if isinstance(items, dict):
            yield from _walk_properties(items, f"{path}[]", depth + 1)


def field_specs(endpoint: EndpointSpec) -> list[FieldSpec]:
    specs: list[FieldSpec] = []
    for param in endpoint.params:
        if param.location in (
            ParamLocation.PATH,
            ParamLocation.QUERY,
            ParamLocation.HEADER,
            ParamLocation.COOKIE,
        ):
            specs.append(FieldSpec(param.location.value, param.name, param.json_schema))
    if endpoint.request_body is not None:
        body_schema = dict(endpoint.request_body.json_schema)
        required = set(body_schema.get("required") or [])
        for path, sub in _walk_properties(body_schema):
            merged = dict(sub)
            if path in required:
                merged["required"] = True
            specs.append(FieldSpec("body", path, merged))
    return specs


@dataclass(frozen=True)
class Observation:
    case_id: str
    present: bool
    value: Any = None


def _lookup(obj: Any, path: str) -> tuple[bool, Any]:
    """按 ``a.b`` / ``items[].sku`` 取嵌套值。数组会展开成多值列表。"""
    nodes: list[Any] = [obj]
    for token in path.split("."):
        is_array = token.endswith("[]")
        key = token[:-2] if is_array else token
        collected: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict) or key not in node:
                continue
            value = node[key]
            if is_array:
                if isinstance(value, list):
                    collected.extend(value)
            else:
                collected.append(value)
        if not collected:
            return False, None
        nodes = collected
    return True, nodes[0] if len(nodes) == 1 else nodes


def path_values(template: str, concrete: str) -> dict[str, str]:
    """从填充后的路径里还原出路径参数的值。"""
    template_parts = template.strip("/").split("/")
    concrete_parts = concrete.strip("/").split("/")
    if len(template_parts) != len(concrete_parts):
        return {}
    values: dict[str, str] = {}
    for template_seg, concrete_seg in zip(template_parts, concrete_parts):
        if template_seg.startswith("{") and template_seg.endswith("}"):
            values[template_seg[1:-1]] = unquote(concrete_seg)
    return values


def observations(cases: Sequence[TestCase], endpoint: EndpointSpec, spec: FieldSpec) -> list[Observation]:
    out: list[Observation] = []
    for case in cases:
        present = False
        value: Any = None
        if spec.location == "body":
            present, value = _lookup(case.body, spec.path)
        elif spec.location == "query":
            present = isinstance(case.query, dict) and spec.path in case.query
            value = case.query.get(spec.path) if present else None
        elif spec.location in ("header", "cookie"):
            lowered = {str(k).lower(): v for k, v in (case.headers or {}).items()}
            present = spec.path.lower() in lowered
            value = lowered.get(spec.path.lower()) if present else None
        elif spec.location == "path":
            values = path_values(endpoint.path, case.path)
            present = spec.path in values
            value = values.get(spec.path) if present else None
        out.append(Observation(case.id, present, value))
    return out


def case_quality(case: TestCase, endpoint: EndpointSpec, *, duplicate: bool) -> int:
    score = 0
    if case.expect.statuses:
        score += 45
    if case.endpoint == endpoint.uid:
        score += 10
    has_payload = bool(case.body) or bool(case.query) or bool(case.headers)
    has_path = bool(path_values(endpoint.path, case.path))
    needs_payload = bool(case.body or case.query or endpoint.request_body is not None)
    if has_payload or has_path or not needs_payload:
        score += 15
    if case.description:
        score += 10
    if len(case.name) >= 6:
        score += 10
    if not duplicate:
        score += 10
    return max(0, min(100, score))


@dataclass(frozen=True)
class RuleOutcome:
    """一条规则的判定结果。"""

    rule_id: str
    title: str
    weight: float
    applicable: bool
    covered: bool
    evidence: tuple[str, ...] = ()
    note: str = ""
    tier: str = "required"


def _not_applicable(
    rule_id: str, title: str, weight: float, note: str, tier: str = "required"
) -> RuleOutcome:
    return RuleOutcome(rule_id, title, weight, False, False, (), note, tier)


def _rule_positive(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    ids = tuple(
        case.id for case in cases if case.expect.statuses and all(s < 400 for s in case.expect.statuses)
    )
    return RuleOutcome(
        "positive_baseline",
        "包含正向用例",
        1.0,
        True,
        bool(ids),
        ids,
        "" if ids else "没有任何一条用例断言成功返回",
    )


def _rule_required_missing(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    if endpoint.request_body is None:
        return _not_applicable("required_missing", "覆盖必填字段缺失", 1.2, "接口无请求体")
    required = endpoint.request_body.json_schema.get("required") or []
    if not required:
        return _not_applicable("required_missing", "覆盖必填字段缺失", 1.2, "请求体没有必填字段")

    ids: list[str] = []
    for case in cases:
        body = case.body if isinstance(case.body, dict) else {}
        absent = [name for name in required if name not in body]
        if absent:
            ids.append(case.id)
    return RuleOutcome(
        "required_missing",
        "覆盖必填字段缺失",
        1.2,
        True,
        bool(ids),
        tuple(ids),
        "" if ids else f"必填字段 {required} 从未被故意省略",
    )


def _rule_param_presence(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    """每个声明过的参数是否至少被一条用例用过。"""
    specs = [
        spec
        for spec in field_specs(endpoint)
        if spec.location != "path" and "." not in spec.path and "[]" not in spec.path
    ]
    if not specs:
        return _not_applicable("param_presence", "覆盖全部声明参数", 1.0, "没有可赋值的参数")

    missing: list[str] = []
    for spec in specs:
        if not any(obs.present for obs in observations(cases, endpoint, spec)):
            missing.append(f"{spec.location}.{spec.path}")

    return RuleOutcome(
        "param_presence",
        "覆盖全部声明参数",
        1.0,
        True,
        not missing,
        (),
        "" if not missing else "从未被赋值: " + ", ".join(missing),
    )


def _rule_business_rule_probe(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    """业务规则探测: 数值业务字段的 0 / 负值。"""

    specs = [
        spec
        for spec in field_specs(endpoint)
        if spec.kind in _NUMERIC_TYPES and spec.location in ("body", "query")
    ]
    if not specs:
        return _not_applicable(
            "business_rule_probe", "覆盖业务规则 (0/负值)", 1.0, "没有可探测的数值业务字段"
        )

    missing: list[str] = []
    ids: list[str] = []
    for spec in specs:
        minimum = spec.schema.get("minimum")
        has_min = isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
        hit = False
        for obs in observations(cases, endpoint, spec):
            value = _as_number(obs.value)
            if value is None:
                continue
            if (has_min and value < float(minimum)) or (not has_min and value <= 0):  # type: ignore[arg-type]
                hit = True
                ids.append(obs.case_id)
        if not hit:
            bound = f"< {minimum}" if has_min else "<= 0"
            missing.append(f"{spec.path} 缺少 {bound} 的取值")

    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "business_rule_probe",
        "覆盖业务规则 (0/负值)",
        1.0,
        True,
        not missing,
        unique,
        "" if not missing else "缺少: " + ", ".join(missing),
    )


def _rule_long_string_probe(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    """字符串字段的超长输入 (没有声明 maxLength 时也要测)。"""
    specs = [
        spec
        for spec in field_specs(endpoint)
        if spec.kind == _STRING_MARKER
        and spec.location in ("body", "query")
        and "maxLength" not in spec.schema
    ]
    if not specs:
        return _not_applicable(
            "long_string_probe", "覆盖超长字符串", 0.6, "没有未声明上限的字符串字段", tier="bonus"
        )

    ids: list[str] = []
    for spec in specs:
        for obs in observations(cases, endpoint, spec):
            if obs.present and isinstance(obs.value, str) and len(obs.value) >= 129:
                ids.append(obs.case_id)
    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "long_string_probe", "覆盖超长字符串", 0.6, True, bool(unique), unique,
        "" if unique else "没有长度 >= 129 的取值", "bonus",
    )


def _rule_length_boundary(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    specs = [s for s in field_specs(endpoint) if "maxLength" in s.schema or "minLength" in s.schema]
    if not specs:
        return _not_applicable("length_boundary", "覆盖长度边界", 1.0, "没有声明长度约束的字段")

    checks: list[tuple[FieldSpec, str, float]] = []
    for spec in specs:
        if isinstance(spec.schema.get("maxLength"), int):
            checks.append((spec, "max", float(spec.schema["maxLength"])))
        if isinstance(spec.schema.get("minLength"), int) and spec.schema["minLength"] > 0:
            checks.append((spec, "min", float(spec.schema["minLength"])))

    ids: list[str] = []
    missing: list[str] = []
    for spec, direction, bound in checks:
        hit = False
        for obs in observations(cases, endpoint, spec):
            if not obs.present or not isinstance(obs.value, str):
                continue
            length = len(obs.value)
            if (direction == "max" and length > bound) or (direction == "min" and length < bound):
                hit = True
                ids.append(obs.case_id)
        if not hit:
            symbol = ">" if direction == "max" else "<"
            missing.append(f"{spec.path} 长度{symbol}{bound:g}")

    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "length_boundary",
        "覆盖长度边界",
        1.0,
        True,
        not missing,
        unique,
        "" if not missing else "缺少: " + ", ".join(missing),
    )


def _rule_numeric_boundary(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    specs = [s for s in field_specs(endpoint) if "minimum" in s.schema or "maximum" in s.schema]
    if not specs:
        return _not_applicable("numeric_boundary", "覆盖数值边界", 1.0, "没有声明数值范围的字段")

    checks: list[tuple[FieldSpec, str, float]] = []
    for spec in specs:
        low, high = spec.schema.get("minimum"), spec.schema.get("maximum")
        if isinstance(low, (int, float)) and not isinstance(low, bool):
            checks.append((spec, "min", float(low)))
        if isinstance(high, (int, float)) and not isinstance(high, bool):
            checks.append((spec, "max", float(high)))

    ids: list[str] = []
    missing: list[str] = []
    for spec, direction, bound in checks:
        hit = False
        for obs in observations(cases, endpoint, spec):
            value = _as_number(obs.value)
            if value is None:
                continue
            if (direction == "min" and value < bound) or (direction == "max" and value > bound):
                hit = True
                ids.append(obs.case_id)
        if not hit:
            symbol = "<" if direction == "min" else ">"
            missing.append(f"{spec.path}{symbol}{bound:g}")

    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "numeric_boundary",
        "覆盖数值边界",
        1.0,
        True,
        not missing,
        unique,
        "" if not missing else "缺少: " + ", ".join(missing),
    )


def _rule_type_error(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    specs = [s for s in field_specs(endpoint) if s.kind in {"integer", "number", "boolean", "string"}]
    if not specs:
        return _not_applicable(
            "type_error", "覆盖类型错误", 0.6, "没有可判定的标量字段", tier="bonus"
        )

    ids: list[str] = []
    for spec in specs:
        for obs in observations(cases, endpoint, spec):
            if not obs.present:
                continue
            if _type_mismatch(spec.kind, obs.value):
                ids.append(obs.case_id)
    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "type_error", "覆盖类型错误", 0.6, True, bool(unique), unique,
        "" if unique else "没有与声明类型不符的取值", "bonus",
    )


def _rule_enum_outside(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    specs = [s for s in field_specs(endpoint) if s.enum]
    if not specs:
        return _not_applicable(
            "enum_outside", "覆盖枚举外值", 0.8, "没有声明 enum 的字段", tier="bonus"
        )

    ids: list[str] = []
    for spec in specs:
        allowed = spec.enum
        for obs in observations(cases, endpoint, spec):
            if obs.present and obs.value not in allowed:
                ids.append(obs.case_id)
    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "enum_outside", "覆盖枚举外值", 0.8, True, bool(unique), unique,
        "" if unique else "没有枚举之外的取值", "bonus",
    )


def _rule_auth(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    """鉴权维度。"""
    header_names = " ".join(
        param.name.lower()
        for param in endpoint.params
        if param.location is ParamLocation.HEADER
    )
    guarded = (
        bool(endpoint.security)
        or "admin" in endpoint.path.lower()
        or any(marker in header_names for marker in ("token", "auth", "api-key", "apikey", "key"))
    )
    if not guarded:
        return _not_applicable("auth", "覆盖鉴权/越权", 1.2, "接口未声明鉴权要求")

    ids = tuple(
        case.id for case in cases if any(code in (401, 403) for code in case.expect.statuses)
    )
    return RuleOutcome(
        "auth", "覆盖鉴权/越权", 1.2, True, bool(ids), ids,
        "" if ids else "没有一条用例断言 401/403",
    )


def _rule_injection(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    specs = [s for s in field_specs(endpoint) if s.kind == _STRING_MARKER]
    if not specs:
        return _not_applicable(
            "injection", "覆盖注入类输入", 1.0, "没有字符串字段可注入", tier="bonus"
        )

    ids: list[str] = []
    for spec in specs:
        for obs in observations(cases, endpoint, spec):
            if obs.present and isinstance(obs.value, str) and _looks_injected(obs.value):
                ids.append(obs.case_id)
    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "injection", "覆盖注入类输入", 1.0, True, bool(unique), unique,
        "" if unique else "字符串字段里没有出现注入特征串", "bonus",
    )


def _rule_empty_string(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleOutcome:
    specs = [
        s for s in field_specs(endpoint) if s.required and s.kind == _STRING_MARKER
    ]
    if not specs:
        return _not_applicable(
            "empty_string", "覆盖空字符串", 0.5, "没有必填的字符串字段", tier="bonus"
        )

    ids: list[str] = []
    for spec in specs:
        for obs in observations(cases, endpoint, spec):
            if obs.present and obs.value == "":
                ids.append(obs.case_id)
    unique = tuple(dict.fromkeys(ids))
    return RuleOutcome(
        "empty_string", "覆盖空字符串", 0.5, True, bool(unique), unique,
        "" if unique else "必填字符串字段没有出现过空串", "bonus",
    )


RULES = (
    _rule_positive,
    _rule_required_missing,
    _rule_param_presence,
    _rule_business_rule_probe,
    _rule_length_boundary,
    _rule_numeric_boundary,
    _rule_type_error,
    _rule_enum_outside,
    _rule_auth,
    _rule_injection,
    _rule_empty_string,
    _rule_long_string_probe,
)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _type_mismatch(declared: str | None, value: Any) -> bool:
    if declared in _NUMERIC_TYPES:
        return _as_number(value) is None
    if declared == "boolean":
        return not isinstance(value, bool)
    if declared == "string":
        return isinstance(value, (int, float, bool))
    return False


def _looks_injected(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in INJECTION_MARKERS)


@dataclass
class RuleEvaluation:
    endpoint: str
    outcomes: list[RuleOutcome] = field(default_factory=list)
    coverage_score: float = 0.0
    required_coverage: float = 0.0
    bonus_coverage: float = 0.0
    case_quality_score: float = 0.0
    duplicate_ratio: float = 0.0
    density_factor: float = 1.0
    rule_score: float = 0.0

    @property
    def applicable(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.applicable]

    @property
    def missed(self) -> list[RuleOutcome]:
        return [o for o in self.applicable if not o.covered]

    @property
    def missed_required(self) -> list[RuleOutcome]:
        return [o for o in self.missed if o.tier == "required"]


def rule_outcomes(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> list[RuleOutcome]:
    return [rule(endpoint, cases) for rule in RULES]


def _weighted(outcomes: Sequence[RuleOutcome], tier: str) -> tuple[float, float]:
    picked = [o for o in outcomes if o.applicable and o.tier == tier]
    total = sum(o.weight for o in picked)
    covered = sum(o.weight for o in picked if o.covered)
    return (covered, total)


def _signature(case: TestCase) -> str:
    return repr(
        (case.path, sorted(case.query.items()), repr(case.body), tuple(case.expect.statuses))
    )


def evaluate_rules(endpoint: EndpointSpec, cases: Sequence[TestCase]) -> RuleEvaluation:
    """端点级规则评分。"""
    outcomes = rule_outcomes(endpoint, cases)

    required_covered, required_total = _weighted(outcomes, "required")
    bonus_covered, bonus_total = _weighted(outcomes, "bonus")

    required_cov = 100.0 * required_covered / required_total if required_total else 0.0
    bonus_cov = 100.0 * bonus_covered / bonus_total if bonus_total else None
    bonus_factor = 0.9 + 0.1 * (bonus_cov / 100.0 if bonus_cov is not None else 1.0)
    coverage = required_cov * bonus_factor

    keys = [_signature(case) for case in cases]
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    duplicates = sum(count - 1 for count in counts.values())
    duplicate_ratio = duplicates / len(cases) if cases else 0.0

    quality = (
        sum(
            case_quality(case, endpoint, duplicate=counts[key] > 1)
            for case, key in zip(cases, keys)
        )
        / len(cases)
        if cases
        else 0.0
    )

    density = min(1.0, len(cases) / 3.0) if cases else 0.0
    effective_coverage = coverage * (1.0 - 0.5 * duplicate_ratio) * density
    return RuleEvaluation(
        endpoint=endpoint.uid,
        outcomes=outcomes,
        coverage_score=round(coverage, 1),
        required_coverage=round(required_cov, 1),
        bonus_coverage=round(bonus_cov, 1) if bonus_cov is not None else 0.0,
        case_quality_score=round(quality, 1),
        duplicate_ratio=round(duplicate_ratio, 3),
        density_factor=round(density, 3),
        rule_score=round(0.75 * effective_coverage + 0.25 * quality, 1),
    )
