"""Swagger 文档版本对比: 找出"这次改了什么"。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .models import ApiSpec, EndpointSpec, ParamLocation

_SCALAR_KEYS = ("required", "type", "format", "nullable", "minimum", "maximum", "maxLength", "minLength")

_BREAKING_KINDS = frozenset({"removed", "required", "type", "enum", "bounds"})
_RELAXING_KINDS = frozenset({"added", "optional", "nullable"})


@dataclass(frozen=True)
class FieldChange:
    location: str
    field_name: str
    kind: str
    before: Any = None
    after: Any = None
    severity: str = "changed"

    def describe(self) -> str:
        if self.kind == "added":
            return f"{self.location}.{self.field_name} 新增字段"
        if self.kind == "removed":
            return f"{self.location}.{self.field_name} 字段被删除"
        if self.kind == "required":
            return f"{self.location}.{self.field_name} 改为必填"
        if self.kind == "optional":
            return f"{self.location}.{self.field_name} 改为非必填"
        if self.kind == "response":
            return f"{self.location}.{self.field_name} 响应状态码变化 {self.before} -> {self.after}"
        return f"{self.location}.{self.field_name} {self.kind} 变化 {self.before!r} -> {self.after!r}"


@dataclass
class EndpointDiff:
    uid: str
    status: str
    changes: list[FieldChange] = field(default_factory=list)

    @property
    def breaking(self) -> list[FieldChange]:
        return [c for c in self.changes if c.severity == "breaking"]

    @property
    def relaxed(self) -> list[FieldChange]:
        return [c for c in self.changes if c.severity == "relaxed"]


@dataclass
class SpecDiff:
    added: list[EndpointDiff] = field(default_factory=list)
    removed: list[EndpointDiff] = field(default_factory=list)
    changed: list[EndpointDiff] = field(default_factory=list)
    unchanged: list[EndpointDiff] = field(default_factory=list)

    @property
    def impacted(self) -> list[EndpointDiff]:
        """需要重新生成用例的接口: 新增 + 变更。"""
        return [*self.added, *self.changed]

    @property
    def impacted_uids(self) -> list[str]:
        return [item.uid for item in self.impacted]

    @property
    def total_endpoints(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed) + len(self.unchanged)

    @property
    def change_points(self) -> list[tuple[str, FieldChange]]:
        """所有字段级变更点, 用于事后检查"增量用例有没有覆盖到每个变更"。"""
        points: list[tuple[str, FieldChange]] = []
        for item in self.changed:
            points.extend((item.uid, change) for change in item.changes)
        for item in self.added:
            points.append((item.uid, FieldChange("endpoint", item.uid, "added")))
        return points

    @property
    def breaking_count(self) -> int:
        return sum(len(item.breaking) for item in self.changed) + len(self.removed)

    @property
    def generation_saving(self) -> float:
        """按接口数粗算的生成量节省比例。"""
        return 1.0 - (len(self.impacted) / self.total_endpoints) if self.total_endpoints else 0.0

    def summary(self) -> str:
        return (
            f"两版共涉及 {self.total_endpoints} 个接口: 新增 {len(self.added)}, "
            f"删除 {len(self.removed)}, 变更 {len(self.changed)}, 未变 {len(self.unchanged)}; "
            f"需重新生成 {len(self.impacted)} 个 (节省 {self.generation_saving:.0%})"
        )

    def render(self, *, limit: int = 40) -> str:
        lines = [self.summary(), ""]
        rows: list[str] = ["| 接口 | 状态 | 变更点 | 严重性 |", "| --- | --- | --- | --- |"]
        for item in [*self.added, *self.removed, *self.changed]:
            if item.status == "added":
                rows.append(f"| `{item.uid}` | 新增 | 新接口 | - |")
                continue
            if item.status == "removed":
                rows.append(f"| `{item.uid}` | 删除 | 调用方需确认下线 | breaking |")
                continue
            details = "; ".join(change.describe() for change in item.changes[:4])
            if len(item.changes) > 4:
                details += f"; 其余 {len(item.changes) - 4} 处"
            rows.append(f"| `{item.uid}` | 变更 | {details} | {item.breaking[0].severity if item.breaking else 'changed'} |")
        lines.extend(rows)
        if self.unchanged:
            lines.append("")
            lines.append(f"未变更 (跳过生成): {', '.join(f'`{item.uid}`' for item in self.unchanged)}")
        return "\n".join(lines[: limit + 4])


def _param_view(param: Any) -> dict[str, Any]:
    view: dict[str, Any] = {"required": bool(param.required)}
    for key in _SCALAR_KEYS[1:]:
        value = param.json_schema.get(key)
        if value is not None:
            view[key] = value
    enum = param.json_schema.get("enum")
    if isinstance(enum, list):
        view["enum"] = sorted(str(item) for item in enum)
    return view


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


def _body_view(endpoint: EndpointSpec) -> dict[str, dict[str, Any]]:
    if endpoint.request_body is None:
        return {}
    schema = endpoint.request_body.json_schema
    required = set(schema.get("required") or [])
    view: dict[str, dict[str, Any]] = {}
    for path, sub in _walk_properties(schema):
        item: dict[str, Any] = {}
        for key in _SCALAR_KEYS:
            value = sub.get(key)
            if value is not None:
                item[key] = value
        if path in required:
            item["required"] = True
        enum = sub.get("enum")
        if isinstance(enum, list):
            item["enum"] = sorted(str(value) for value in enum)
        view[path] = item
    return view


def _views(endpoint: EndpointSpec) -> dict[tuple[str, str], dict[str, Any]]:
    """(location, field) -> 属性视图。"""
    views: dict[tuple[str, str], dict[str, Any]] = {}
    for param in endpoint.params:
        views[(param.location.value, param.name)] = _param_view(param)
    for path, item in _body_view(endpoint).items():
        views[("body", path)] = item
    return views


def _response_codes(endpoint: EndpointSpec) -> set[str]:
    return {response.status_code for response in endpoint.responses}


def _direction(before: Any, after: Any, *, upper: bool) -> str:
    """边界类变化的方向: 收紧(breaking) / 放宽(relaxed)。"""
    if before is None and after is None:
        return "changed"
    if before is None:
        return "breaking"
    if after is None:
        return "relaxed"
    try:
        old, new = float(before), float(after)
    except (TypeError, ValueError):
        return "changed"
    tightened = new < old if upper else new > old
    return "breaking" if tightened else "relaxed"


def _severity(kind: str, before: Any, after: Any) -> str:
    if kind in {"removed", "required", "type"}:
        return "breaking"
    if kind in {"added", "optional", "nullable"}:
        return "relaxed"
    if kind == "enum":
        old = set(before or [])
        new = set(after or [])
        if old and new and new < old:
            return "breaking"
        if old and new and new > old:
            return "relaxed"
        return "changed"
    if kind in {"maximum", "maxLength"}:
        return _direction(before, after, upper=True)
    if kind in {"minimum", "minLength"}:
        return _direction(before, after, upper=False)
    return "changed"


def _diff_views(
    location: str, field_name: str, before: dict[str, Any], after: dict[str, Any]
) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        kind = "required" if (key == "required" and new) else ("optional" if key == "required" else key)
        changes.append(
            FieldChange(
                location=location,
                field_name=field_name,
                kind=kind,
                before=old,
                after=new,
                severity=_severity(kind, old, new),
            )
        )
    return changes


def _diff_endpoint(before: EndpointSpec, after: EndpointSpec) -> list[FieldChange]:
    changes: list[FieldChange] = []
    old_views, new_views = _views(before), _views(after)

    for key in new_views.keys() - old_views.keys():
        changes.append(
            FieldChange(key[0], key[1], "added", severity="relaxed")
        )
    for key in old_views.keys() - new_views.keys():
        changes.append(FieldChange(key[0], key[1], "removed", severity="breaking"))
    for key in old_views.keys() & new_views.keys():
        changes.extend(_diff_views(key[0], key[1], old_views[key], new_views[key]))

    removed_codes = _response_codes(before) - _response_codes(after)
    added_codes = _response_codes(after) - _response_codes(before)
    for code in sorted(removed_codes | added_codes):
        changes.append(
            FieldChange(
                "response",
                code,
                "response",
                before=code in _response_codes(before),
                after=code in _response_codes(after),
                severity="breaking" if code in removed_codes else "relaxed",
            )
        )
    return changes


def diff_specs(old: ApiSpec, new: ApiSpec) -> SpecDiff:
    """对比两版文档, 产出结构化的变更清单。"""
    old_index = old.endpoint_index()
    new_index = new.endpoint_index()
    result = SpecDiff()

    for uid, endpoint in new_index.items():
        if uid not in old_index:
            result.added.append(EndpointDiff(uid=uid, status="added"))
            continue
        changes = _diff_endpoint(old_index[uid], endpoint)
        if changes:
            result.changed.append(EndpointDiff(uid=uid, status="changed", changes=changes))
        else:
            result.unchanged.append(EndpointDiff(uid=uid, status="unchanged"))

    for uid in old_index.keys() - new_index.keys():
        result.removed.append(EndpointDiff(uid=uid, status="removed"))

    for bucket in (result.added, result.changed, result.removed, result.unchanged):
        bucket.sort(key=lambda item: item.uid)
    return result


def changed_fields(diff: SpecDiff) -> dict[str, set[tuple[str, str]]]:
    """接口 -> {(location, field)} , 用于检查增量用例是否碰到了每个变更点。"""
    out: dict[str, set[tuple[str, str]]] = {}
    for item in diff.changed:
        out[item.uid] = {
            (change.location, change.field_name)
            for change in item.changes
            if change.location != "response"
        }
    return out
