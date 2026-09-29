"""检定执行：只负责掷骰与成功度判定，不做任何规则语义。

与 ``src/engine/dice_rng`` 的区别：这里**必须**使用调用方注入的 RNG
（``ruleset_gameplay`` 传的是 ``random.SystemRandom()``），不碰模块级
``random``，保证权威结算走服务端 RNG 且可被外部替换（测试可注入确定性 RNG）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.rulesets.custom.manifest import CheckSpec, DegreeSpec

_FORMULA_RE = re.compile(r"^(\d+)?d(\d+)([+-]\d+)?$")


@dataclass(frozen=True, slots=True)
class RollOutcome:
    formula: str
    rolls: tuple[int, ...]
    modifier: int
    total: int


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    check_id: str
    check_name: str
    roll: RollOutcome
    target: int
    comparison: str
    degree_id: str
    degree_label: str
    is_success: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "check_name": self.check_name,
            "formula": self.roll.formula,
            "rolls": list(self.roll.rolls),
            "modifier": self.roll.modifier,
            "total": self.roll.total,
            "target": self.target,
            "comparison": self.comparison,
            "degree": self.degree_id,
            "degree_label": self.degree_label,
            "is_success": self.is_success,
        }


def roll_formula(rng: Any, formula: str) -> RollOutcome:
    """按 ``NdM`` / ``NdM±K`` 掷骰。``rng`` 需提供 ``randint(a, b)``。"""

    normalized = str(formula or "").strip().lower().replace(" ", "")
    match = _FORMULA_RE.fullmatch(normalized)
    if not match:
        raise ValueError(f"无效的掷骰公式: {formula!r}")
    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    modifier = int(match.group(3) or 0)
    if count < 1 or count > 100:
        raise ValueError(f"骰子数量超出范围: {formula!r}")
    if sides < 2 or sides > 10000:
        raise ValueError(f"骰子面数超出范围: {formula!r}")
    if abs(modifier) > 100:
        raise ValueError(f"骰子修正超出范围: {formula!r}")
    rolls = tuple(int(rng.randint(1, sides)) for _ in range(count))
    return RollOutcome(
        formula=normalized, rolls=rolls, modifier=modifier,
        total=sum(rolls) + modifier,
    )


def _ratio(total: int, target: int, comparison: str) -> float:
    """把掷值与目标值折算成"越小越好"的比值，用于成功度分级。

    - ``lte``：``total / target``（CoC 式：掷得越低越好）
    - ``gte``：``target / total``（掷得越高越好）

    目标值 <= 0 时按最差处理，避免除零。
    """

    if comparison == "gte":
        if total <= 0:
            return float("inf")
        return max(0.0, target) / total
    if target <= 0:
        return float("inf")
    return total / target


def _pick_degree(degrees: tuple[DegreeSpec, ...], ratio: float) -> DegreeSpec:
    for degree in degrees:
        if degree.fallback:
            return degree
        if ratio <= degree.max_ratio:
            return degree
    # 声明解析已保证末尾是 fallback，这里只是兜底。
    return degrees[-1]


def resolve_check(
    rng: Any,
    check: CheckSpec,
    *,
    target: int,
) -> CheckOutcome:
    """执行一次检定并返回成功度。同一检定只掷一次骰。"""

    roll = roll_formula(rng, check.dice)
    ratio = _ratio(roll.total, target, check.comparison)
    if check.comparison == "gte":
        is_success = roll.total >= target
    else:
        is_success = roll.total <= target
    degree = _pick_degree(check.degrees, ratio)
    return CheckOutcome(
        check_id=check.id,
        check_name=check.name,
        roll=roll,
        target=int(target),
        comparison=check.comparison,
        degree_id=degree.id,
        degree_label=degree.label,
        is_success=is_success,
    )


def parse_delta(rng: Any, delta: str) -> int:
    """把效果声明里的 ``delta`` 解析成整数增量（``"3"`` / ``"-1d6"``）。"""

    text = str(delta or "").strip().lower()
    if not text:
        return 0
    sign = 1
    if text[0] in "+-":
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    if "d" in text:
        outcome = roll_formula(rng, f"1d{text.split('d', 1)[1]}")
        return sign * outcome.total
    return sign * int(text)


__all__ = ["CheckOutcome", "RollOutcome", "parse_delta", "resolve_check", "roll_formula"]
