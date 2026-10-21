"""一致性度量: 规则分/模型分 到底能不能替代人工判断?"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class LabelPair:
    key: str
    rule_score: float
    human_score: float
    rule_pass: bool
    human_pass: bool

    @property
    def delta(self) -> float:
        return self.rule_score - self.human_score

    @property
    def agree_on_pass(self) -> bool:
        return self.rule_pass == self.human_pass


class LabeledScore(BaseModel):
    """人工标注文件里的一条记录。"""

    model_config = ConfigDict(extra="ignore")

    suite: str
    endpoint: str
    human_score: float
    human_pass: bool
    note: str | None = None


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2 + 1
        for position in range(index, end + 1):
            ranks[order[position]] = average
        index = end + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n == 0:
        return 0.0
    mean_x, mean_y = sum(x) / n, sum(y) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    var_x = sum((a - mean_x) ** 2 for a in x)
    var_y = sum((b - mean_y) ** 2 for b in y)
    if var_x <= 0 or var_y <= 0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


def spearman(x: list[float], y: list[float]) -> float:
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    return round(_pearson(_ranks(x), _ranks(y)), 3)


def mae(x: list[float], y: list[float]) -> float:
    if not x or len(x) != len(y):
        return 0.0
    return round(sum(abs(a - b) for a, b in zip(x, y)) / len(x), 2)


def cohen_kappa(a: list[int], b: list[int]) -> float:
    """两个评分者的一致性。``1.0`` 完全一致, ``0`` 等于随机。"""
    n = len(a)
    if n == 0 or len(b) != n:
        return 0.0
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    pa, pb = sum(a) / n, sum(b) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    if expected >= 1:
        return 0.0
    return round((observed - expected) / (1 - expected), 3)


def kappa_sweep(pairs: list[LabelPair], thresholds: list[float] | None = None) -> list[tuple[float, float, float]]:
    """扫描不同通过阈值下的一致性。"""
    if thresholds is None:
        thresholds = [60, 65, 70, 75, 80, 85, 90, 95]
    results: list[tuple[float, float, float]] = []
    for threshold in thresholds:
        rule_flags = [1 if pair.rule_score >= threshold else 0 for pair in pairs]
        human_flags = [1 if pair.human_pass else 0 for pair in pairs]
        results.append(
            (
                float(threshold),
                cohen_kappa(rule_flags, human_flags),
                sum(1 for a, b in zip(rule_flags, human_flags) if a == b) / len(pairs)
                if pairs
                else 0.0,
            )
        )
    return results


@dataclass
class AgreementReport:
    pairs: list[LabelPair] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.pairs)

    @property
    def spearman(self) -> float:
        return spearman([p.rule_score for p in self.pairs], [p.human_score for p in self.pairs])

    @property
    def mae(self) -> float:
        return mae([p.rule_score for p in self.pairs], [p.human_score for p in self.pairs])

    @property
    def kappa(self) -> float:
        return cohen_kappa(
            [1 if p.rule_pass else 0 for p in self.pairs],
            [1 if p.human_pass else 0 for p in self.pairs],
        )

    @property
    def pass_agreement(self) -> float:
        if not self.pairs:
            return 0.0
        return sum(1 for p in self.pairs if p.agree_on_pass) / len(self.pairs)

    def disagreements(self, limit: int = 5) -> list[LabelPair]:
        return sorted(self.pairs, key=lambda p: -abs(p.delta))[:limit]

    def verdict(self) -> str:
        kappa = self.kappa
        if kappa >= 0.8:
            return "高度一致, 规则分可无人值守"
        if kappa >= 0.6:
            return "基本一致, 可作为分流依据, 边界样本仍需人工抽查"
        if kappa >= 0.4:
            return "中等一致, 只能当辅助, 不能替代人工"
        return "一致性不足, 评分规则本身需要校准"

    def render(self) -> str:
        lines = [
            f"标注样本 n={self.n}",
            f"- Spearman ρ (排序一致性): {self.spearman}",
            f"- MAE (分数平均偏差): {self.mae}",
            f"- Cohen's κ (通过/不通过一致性): {self.kappa}",
            f"- 分流决策一致率: {self.pass_agreement:.1%}",
            f"- 结论: {self.verdict()}",
        ]
        if self.notes:
            lines += ["", *[f"> {note}" for note in self.notes]]
        if self.pairs:
            lines += ["", "| 接口 | 规则分 | 人工分 | 差值 | 分流一致 |", "| --- | --- | --- | --- | --- |"]
            for pair in self.pairs:
                mark = "是" if pair.agree_on_pass else "**否**"
                lines.append(
                    f"| `{pair.key}` | {pair.rule_score:.1f} | {pair.human_score:.1f} | "
                    f"{pair.delta:+.1f} | {mark} |"
                )
        return "\n".join(lines)


def build_report(
    pairs: list[LabelPair],
    *,
    notes: list[str] | None = None,
) -> AgreementReport:
    return AgreementReport(pairs=pairs, notes=notes or [])
