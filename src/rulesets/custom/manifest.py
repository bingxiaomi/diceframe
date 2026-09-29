"""``custom_mechanics`` 声明的解析与归一化。

规则模板里的这一段由规则作者自由编写，本模块只负责把它变成运行时可靠的值。
**这里没有机制枚举白名单**：声明什么资源、什么检定、什么成功度分级，运行时
就执行什么。这正是不走 ``check_mechanic`` 的意义 —— 加新机制不需要改引擎代码。

声明格式（全部字段都在规则模板 JSON 内，可随规则继承 ``extends`` 覆盖）::

    "custom_mechanics": {
      "resources": [
        {"id": "sanity", "name": "理智", "initial": {"attribute": "pow"},
         "max": {"attribute": "pow"}, "default": 50}
      ],
      "checks": [
        {"id": "sanity_check", "name": "理智检定", "dice": "1d100",
         "comparison": "lte",
         "target": {"resource": "sanity"},
         "degrees": [
           {"id": "extreme", "label": "极难成功", "max_ratio": 0.2},
           {"id": "hard", "label": "困难成功", "max_ratio": 0.5},
           {"id": "success", "label": "成功", "max_ratio": 1.0},
           {"id": "failure", "label": "失败", "fallback": true}
         ]}
      ],
      "effects": [
        {"check": "sanity_check", "degree": "failure",
         "resource": "sanity", "delta": "-1d6"}
      ],
      "world": [
        {"id": "sealed_door", "kind": "door",
         "state": {"locked": true, "open": false, "hp": 20},
         "tags": ["wooden", "interactable"],
         "presentation": {"description": "一扇被某种力量封住的门。"}}
      ],
      "world_effects": [
        {"check": "sanity_check", "degree": "extreme",
         "target": "sealed_door", "field": "state.locked",
         "op": "override", "value": false},
        {"check": "sanity_check", "degree": "failure",
         "target": "sealed_door", "field": "state.hp",
         "op": "subtract", "delta": "1d6"}
      ],
      "authoritative_fields": ["resources", "attributes", "skills"]
    }

``world`` 与 ``world_effects`` 的约定：

- ``world`` 只是**初始**对象表。首次用到世界状态时插入，之后它就是权威状态，
  **不会重播声明** —— 否则已经解锁的门会在下一局又被声明锁上。
- ``world_effects[*].target`` 必须在 ``world`` 里声明过（fail-fast 抓拼写错误）。
- ``field`` 只能写 ``state.*`` 与 ``tags``。``presentation.*`` **写不进去**：
  呈现只能由权威状态派生，反向推导会让叙事污染事实。
- ``op`` 取 Foundry 的 change 词汇；``override`` 直接取 ``value``，
  其余数值算子取 ``value``（字面值）或 ``delta``（骰式，用服务端 RNG 解析）。

``target`` / ``initial`` / ``max`` 的取值引用语法：

- ``{"attribute": "pow"}``      读角色属性
- ``{"special_stat": "luck"}``  读角色特殊值
- ``{"resource": "sanity"}``    读本运行时追踪的资源
- ``{"constant": 50}``          固定值

解析失败一律 fail-fast 抛 ``ValueError``，不静默降级 —— 规则写错应该在加载
或建卡时立刻暴露，而不是在跑团中途变成"莫名其妙不掷骰"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.rulesets.adjudication import (
    EFFECT_CHANGE_OPS,
    ConsequenceSeverity,
    ConsumptionTiming,
    SeverityScale,
)
from src.rulesets.attempts import RetryPolicy
from src.rulesets.effects import DURATION_KINDS

MAX_RESOURCES = 24
MAX_CHECKS = 48
MAX_DEGREES = 8
MAX_EFFECTS = 64
MAX_WORLD_OBJECTS = 32
MAX_WORLD_EFFECTS = 64

#: 世界效果允许的字段根前缀。``presentation`` 刻意不在其中。
_WORLD_FIELD_ROOTS = ("state.", "tags")

#: 角色效果禁止写的字段根前缀。
_EFFECT_FORBIDDEN_PREFIXES = ("presentation",)

#: 严重度标尺用不可变对象做默认值，不用 ``field(default_factory=...)``。
_NO_SEVERITY = SeverityScale()

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_COMPARISONS = frozenset({"lte", "gte"})
_VALUE_OPS = frozenset({"attribute", "special_stat", "resource", "constant"})

# 资源与检定 id 前缀，避免和通用状态标签撞名。
RESOURCE_PREFIX = "custom_resource:"


@dataclass(frozen=True, slots=True)
class ValueRef:
    """一个值引用：``op`` 决定 ``key`` 的含义。"""

    op: str
    key: str = ""
    constant: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "key": self.key, "constant": self.constant}

    @staticmethod
    def from_dict(raw: Any, *, field: str) -> "ValueRef":
        if isinstance(raw, bool):
            raise ValueError(f"{field} 不支持布尔值")
        if isinstance(raw, int):
            return ValueRef(op="constant", constant=raw)
        if isinstance(raw, str):
            # 允许 "pow" / "@pow" 这种简写，等价于 {"attribute": "pow"}。
            key = raw.strip().lstrip("@")
            if not key:
                raise ValueError(f"{field} 不能为空字符串")
            return ValueRef(op="attribute", key=key)
        if not isinstance(raw, dict):
            raise ValueError(f"{field} 必须是整数、字符串或引用对象")
        ops = [op for op in raw if op in _VALUE_OPS]
        if len(ops) != 1:
            raise ValueError(f"{field} 必须且只能包含 {sorted(_VALUE_OPS)} 之一")
        op = ops[0]
        if op == "constant":
            value = raw[op]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{field}.constant 必须是整数")
            return ValueRef(op="constant", constant=value)
        key = str(raw[op] or "").strip()
        if not key:
            raise ValueError(f"{field}.{op} 不能为空")
        return ValueRef(op=op, key=key)

    def resolve(self, *, attributes: dict[str, int], special_stats: dict[str, int],
                resources: dict[str, int]) -> int | None:
        """求值；引用的键不存在时返回 ``None``（调用方决定回落策略）。"""

        if self.op == "constant":
            return self.constant
        if self.op == "attribute":
            return attributes.get(self.key)
        if self.op == "special_stat":
            return special_stats.get(self.key)
        if self.op == "resource":
            return resources.get(self.key)
        return None


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    id: str
    name: str
    initial: ValueRef
    maximum: ValueRef | None
    default: int


@dataclass(frozen=True, slots=True)
class DegreeSpec:
    """成功度分级。``fallback=True`` 的档位必须且只能有一个，且放在最后。

    ``achieved``/``extent`` 把"成没成"与"成了多少"分开。不写就从结构派生：
    **兜底档就是失败档**（解析已保证它唯一且在最后），所以其余档位默认
    达成、程度 1.0。要表达"部分成功"就写 ``"extent": 0.5`` —— 不需要
    发明一个新的档位名，也不需要宿主认识那个名字。
    """

    id: str
    label: str
    max_ratio: float
    fallback: bool
    #: ``None`` = 从结构派生（兜底档=False，其余=True）。
    achieved: bool | None = None
    #: ``None`` = 从结构派生（兜底档=0.0，其余=1.0）。
    extent: float | None = None

    @property
    def goal_achieved(self) -> bool:
        return (not self.fallback) if self.achieved is None else self.achieved

    @property
    def goal_extent(self) -> float:
        if self.extent is not None:
            return self.extent
        return 0.0 if self.fallback else 1.0


@dataclass(frozen=True, slots=True)
class CheckSpec:
    id: str
    name: str
    dice: str
    comparison: str
    target: ValueRef
    degrees: tuple[DegreeSpec, ...]
    #: 三问法里**掷骰前**就能回答的两问（加第三问）。掷骰前还不知道成功度，
    #: 所以这三项只能由规则作者表态，不能从世界状态推。
    can_succeed: bool = True
    can_fail: bool = True
    failure_matters: bool = True
    #: 是否让**尝试账本**参与骰前判定（账本可用尽则不再掷骰）。
    #: 默认关："试过了还要不要掷"是桌风问题，由规则作者表态，
    #: 否则会给已有规则静默加上行为变化。
    consult_attempts: bool = False
    #: 后果严重度标尺（可选）。缺省 = 不产生严重度（``NONE``）。
    #: **与 ``Risk`` 是两个轴**：风险动掷骰难度，严重度动"失败之后有多疼"。
    severity: SeverityScale = _NO_SEVERITY

    @property
    def degree_order(self) -> tuple[tuple[str, bool], ...]:
        """从好到坏排列的 ``(id, achieved)``。

        严重度靠"差几档"算，不靠档位名 —— 否则每套自定义命名都得配一个公式。
        """

        return tuple((d.id, d.goal_achieved) for d in self.degrees)

    @property
    def failure_degree_id(self) -> str:
        """失败档的 id。

        解析阶段已经保证 ``fallback`` 档位存在、唯一、且在最后，所以那个档
        就是失败档 —— 不需要另立一处声明，也就不会与实际分级漂移。
        """

        return self.degrees[-1].id

    @property
    def can_ever_roll(self) -> bool:
        """这个检定是否**有可能**真的掷骰。

        三个骰前问题里任一为否，就会无条件短路（不掷骰），成功度也就不存在。
        ``consult_attempts`` 不在其中：它是**有条件**短路（看账本），仍会掷骰。
        """

        return self.can_succeed and self.can_fail and self.failure_matters


@dataclass(frozen=True, slots=True)
class ConsumptionSpec:
    """一次检定的代价：扣哪种资源、扣多少、什么时候扣。

    ``amount`` 必须是**正数**。负数的"消耗"其实是奖励，那是 ``effects``
    该干的事；混在一起会让"资源不足"这个判断失去意义。
    """

    check: str
    resource: str
    amount: int
    timing: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class EffectSpec:
    check: str
    degree: str
    resource: str
    delta: str


@dataclass(frozen=True, slots=True)
class WorldObjectSpec:
    """初始世界对象。**只用于首次播种**，之后权威状态在存档里。"""

    id: str
    kind: str
    state: dict[str, Any]
    tags: tuple[str, ...]
    presentation: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorldEffectSpec:
    """检定结果 → 世界状态变更。

    ``value`` 与 ``delta`` 二选一：``override`` 用 ``value``（字面值，
    非 null）；其余数值算子用 ``value`` 或 ``delta``（骰式，用服务端 RNG 解析）。
    """

    check: str
    degree: str
    target: str
    field: str
    op: str
    value: Any = None
    delta: str = ""
    #: 尝试记录（可选）。声明了 ``intent_family`` 才会把"试过了"记下来 ——
    #: 不声明就是纯粹的世界变更，与重试无关。
    intent_family: str = ""
    approach_signature: str = ""
    retry_policy: str = ""


@dataclass(frozen=True, slots=True)
class CharacterEffectSpec:
    """检定结果 → 给行动者挂一个活跃效果（基础值 + 效果 = 有效值）。

    效果**永远不直接改基础数值**：它只往 ``项目`` 里加一条带来源与时长的
    修正量，有效值在读取时算出来。
    """

    check: str
    degree: str
    id: str
    label: str
    changes: tuple[tuple[str, str, Any], ...]
    duration: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CustomMechanics:
    resources: tuple[ResourceSpec, ...]
    checks: tuple[CheckSpec, ...]
    effects: tuple[EffectSpec, ...]
    authoritative_fields: tuple[str, ...]
    world: tuple[WorldObjectSpec, ...] = ()
    world_effects: tuple[WorldEffectSpec, ...] = ()
    character_effects: tuple[CharacterEffectSpec, ...] = ()
    consumptions: tuple[ConsumptionSpec, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.resources or self.checks or self.effects)

    def consumptions_for(self, check_id: str) -> tuple[ConsumptionSpec, ...]:
        return tuple(spec for spec in self.consumptions if spec.check == check_id)

    def action_keys(self, check_id: str) -> tuple[tuple[str, str, str], ...]:
        """某个检定关联的 ``(intent_family, approach_signature, target)`` 组合。

        从 ``world_effects`` **派生**，不是另一处声明 —— 骰前判定与尝试记账
        必须看同一组 key，否则会出现"记了但查不到"。
        """

        seen: dict[tuple[str, str, str], None] = {}
        for spec in self.world_effects:
            if spec.check != check_id or not spec.intent_family:
                continue
            seen.setdefault(
                (spec.intent_family, spec.approach_signature, spec.target), None,
            )
        return tuple(seen)

    def world_seed(self) -> dict[str, dict[str, Any]]:
        """初始对象表（JSON 安全），供首次播种使用。

        ``tags`` 排序：保证刚播种的世界与经过一次读写的世界表示一致，
        否则落盘结果会依赖声明顺序，diff 与测试都不稳定。
        """

        return {
            spec.id: {
                "kind": spec.kind,
                "state": dict(spec.state),
                "tags": sorted(spec.tags),
                "presentation": dict(spec.presentation),
                "version": 0,
            }
            for spec in self.world
        }


EMPTY = CustomMechanics(resources=(), checks=(), effects=(), authoritative_fields=())


def _require_id(raw: Any, *, field: str) -> str:
    value = str(raw or "").strip().lower()
    if not _ID_RE.match(value):
        raise ValueError(f"{field} 必须匹配 {_ID_RE.pattern}: {raw!r}")
    return value


def _parse_resources(raw: Any) -> tuple[ResourceSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_mechanics.resources 必须是数组")
    if len(raw) > MAX_RESOURCES:
        raise ValueError(f"custom_mechanics.resources 最多 {MAX_RESOURCES} 项")
    specs: list[ResourceSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"custom_mechanics.resources[{index}] 必须是对象")
        res_id = _require_id(item.get("id"), field=f"resources[{index}].id")
        if res_id in seen:
            raise ValueError(f"资源 id 重复: {res_id}")
        seen.add(res_id)
        default = item.get("default", 0)
        if isinstance(default, bool) or not isinstance(default, int):
            raise ValueError(f"resources[{index}].default 必须是整数")
        specs.append(ResourceSpec(
            id=res_id,
            name=str(item.get("name") or res_id).strip()[:32],
            initial=ValueRef.from_dict(item.get("initial", default), field=f"resources[{index}].initial"),
            maximum=(
                ValueRef.from_dict(item["max"], field=f"resources[{index}].max")
                if item.get("max") is not None else None
            ),
            default=default,
        ))
    return tuple(specs)


def _parse_achieved(item: dict[str, Any], *, where: str) -> bool | None:
    if "achieved" not in item:
        return None
    value = item["achieved"]
    if not isinstance(value, bool):
        raise ValueError(f"{where}.achieved 必须是布尔值")
    return value


def _parse_extent(item: dict[str, Any], *, where: str) -> float | None:
    if "extent" not in item:
        return None
    value = item["extent"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}.extent 必须是数字")
    extent = float(value)
    if not 0.0 <= extent <= 1.0:
        raise ValueError(f"{where}.extent 必须在 [0, 1] 之间")
    return extent


def _parse_degrees(raw: Any, *, field: str) -> tuple[DegreeSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} 必须是非空数组")
    if len(raw) > MAX_DEGREES:
        raise ValueError(f"{field} 最多 {MAX_DEGREES} 档")
    degrees: list[DegreeSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{field}[{index}] 必须是对象")
        degree_id = _require_id(item.get("id"), field=f"{field}[{index}].id")
        if degree_id in seen:
            raise ValueError(f"{field} 档位 id 重复: {degree_id}")
        seen.add(degree_id)
        fallback = bool(item.get("fallback"))
        ratio = 0.0
        if not fallback:
            raw_ratio = item.get("max_ratio")
            if isinstance(raw_ratio, bool) or not isinstance(raw_ratio, (int, float)):
                raise ValueError(f"{field}[{index}].max_ratio 必须是数字（或设 fallback: true）")
            ratio = float(raw_ratio)
            if not 0 < ratio <= 1.0:
                raise ValueError(f"{field}[{index}].max_ratio 必须在 (0, 1] 之间")
        degrees.append(DegreeSpec(
            id=degree_id,
            label=str(item.get("label") or degree_id).strip()[:32],
            max_ratio=ratio,
            fallback=fallback,
            achieved=_parse_achieved(item, where=f"{field}[{index}]"),
            extent=_parse_extent(item, where=f"{field}[{index}]"),
        ))
    fallbacks = [d for d in degrees if d.fallback]
    if len(fallbacks) != 1:
        raise ValueError(f"{field} 必须且只能有一个 fallback: true 档位")
    if not degrees[-1].fallback:
        raise ValueError(f"{field} 的 fallback 档位必须放在最后")
    ratios = [d.max_ratio for d in degrees if not d.fallback]
    if ratios != sorted(ratios):
        raise ValueError(f"{field} 的 max_ratio 必须从小到大排列")
    fallback_degree = degrees[-1]
    if fallback_degree.achieved is True and fallback_degree.extent is None:
        # 兜底档声明为"达成"是合法的（"失败但仍有收获"），但必须写清楚程度 ——
        # 否则它会拿到派生默认的 extent 0.0，变成"达成 0%"这种自相矛盾的组合。
        raise ValueError(
            f"{field} 的兜底档 {fallback_degree.id!r} 声明了 achieved: true，"
            "必须同时给出 extent（否则默认 0.0，等于「达成 0%」）"
        )
    return tuple(degrees)


def _parse_checks(raw: Any) -> tuple[CheckSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_mechanics.checks 必须是数组")
    if len(raw) > MAX_CHECKS:
        raise ValueError(f"custom_mechanics.checks 最多 {MAX_CHECKS} 项")
    specs: list[CheckSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"custom_mechanics.checks[{index}] 必须是对象")
        check_id = _require_id(item.get("id"), field=f"checks[{index}].id")
        if check_id in seen:
            raise ValueError(f"检定 id 重复: {check_id}")
        seen.add(check_id)
        comparison = str(item.get("comparison") or "lte").strip().lower()
        if comparison not in _COMPARISONS:
            raise ValueError(
                f"checks[{index}].comparison 必须是 {sorted(_COMPARISONS)} 之一: {comparison!r}"
            )
        dice = str(item.get("dice") or "1d100").strip().lower()
        if not re.fullmatch(r"\d+d\d+([+-]\d+)?", dice):
            raise ValueError(f"checks[{index}].dice 必须是 NdM 或 NdM±K 形式: {dice!r}")
        specs.append(CheckSpec(
            id=check_id,
            name=str(item.get("name") or check_id).strip()[:32],
            dice=dice,
            comparison=comparison,
            target=ValueRef.from_dict(
                item.get("target", {"constant": 50}), field=f"checks[{index}].target",
            ),
            degrees=_parse_degrees(item.get("degrees"), field=f"checks[{index}].degrees"),
            **_parse_adjudication(item.get("adjudication"), where=f"checks[{index}].adjudication"),
        ))
    return tuple(specs)


_ADJUDICATION_KEYS = (
    "can_succeed", "can_fail", "failure_matters", "consult_attempts",
    "severity", "severity_per_step",
)


def _parse_severity_scale(raw: dict[str, Any], *, where: str) -> SeverityScale:
    """解析 ``severity`` / ``severity_per_step``。

    基础严重度是**这条规则认为"失败"该有多疼**；具体多疼由差了几档再加权。
    """

    if "severity" not in raw and "severity_per_step" not in raw:
        return _NO_SEVERITY
    base = _NO_SEVERITY.base
    if "severity" in raw:
        raw_base = str(raw["severity"] or "").strip().upper()
        try:
            base = ConsequenceSeverity(raw_base)
        except ValueError as exc:
            raise ValueError(
                f"{where}.severity 必须是 "
                f"{[str(s) for s in ConsequenceSeverity]} 之一: {raw_base!r}"
            ) from exc
    per_step = _NO_SEVERITY.per_step
    if "severity_per_step" in raw:
        raw_step = raw["severity_per_step"]
        if isinstance(raw_step, bool) or not isinstance(raw_step, int):
            raise ValueError(f"{where}.severity_per_step 必须是整数")
        if raw_step < 0:
            raise ValueError(f"{where}.severity_per_step 不能为负")
        per_step = raw_step
    return SeverityScale(base=base, per_step=per_step)


def _parse_adjudication(raw: Any, *, where: str) -> dict[str, Any]:
    """解析检定上的 ``adjudication`` 块（可选）。

    前四项决定**要不要掷骰**，而掷骰前还不知道成功度，所以必须由规则作者
    表态；缺省全部 ``true`` = 保持"总是骰"的原行为。后两项是后果严重度。
    """

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where} 必须是对象")
    unknown = sorted(set(raw) - set(_ADJUDICATION_KEYS))
    if unknown:
        raise ValueError(
            f"{where} 含未知字段 {unknown}（只支持 {list(_ADJUDICATION_KEYS)}）"
        )
    parsed: dict[str, Any] = {}
    for key in ("can_succeed", "can_fail", "failure_matters", "consult_attempts"):
        if key not in raw:
            continue
        if not isinstance(raw[key], bool):
            raise ValueError(f"{where}.{key} 必须是布尔值")
        parsed[key] = raw[key]
    scale = _parse_severity_scale(raw, where=where)
    if scale != _NO_SEVERITY:
        parsed["severity"] = scale
    return parsed


def _parse_effects(raw: Any, *, checks: tuple[CheckSpec, ...],
                  resources: tuple[ResourceSpec, ...]) -> tuple[EffectSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_mechanics.effects 必须是数组")
    if len(raw) > MAX_EFFECTS:
        raise ValueError(f"custom_mechanics.effects 最多 {MAX_EFFECTS} 项")
    check_ids = {c.id for c in checks}
    degree_ids = {d.id for c in checks for d in c.degrees}
    resource_ids = {r.id for r in resources}
    specs: list[EffectSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"custom_mechanics.effects[{index}] 必须是对象")
        check_id = _require_id(item.get("check"), field=f"effects[{index}].check")
        if check_id not in check_ids:
            raise ValueError(f"effects[{index}].check 引用了未声明的检定: {check_id}")
        degree_id = _require_id(item.get("degree"), field=f"effects[{index}].degree")
        if degree_id not in degree_ids:
            raise ValueError(f"effects[{index}].degree 引用了未声明的成功度: {degree_id}")
        resource_id = _require_id(item.get("resource"), field=f"effects[{index}].resource")
        if resource_id not in resource_ids:
            raise ValueError(f"effects[{index}].resource 引用了未声明的资源: {resource_id}")
        delta = str(item.get("delta") or "").strip().lower()
        # delta 允许 "3" / "-3" / "1d6" / "-1d6"；符号决定增减方向。
        if not re.fullmatch(r"[+-]?\d+(d\d+)?", delta):
            raise ValueError(f"effects[{index}].delta 必须是整数或骰式（可带符号）: {delta!r}")
        specs.append(EffectSpec(
            check=check_id, degree=degree_id, resource=resource_id, delta=delta,
        ))
    return tuple(specs)


def _parse_world(raw: Any) -> tuple[WorldObjectSpec, ...]:
    """解析 ``custom_mechanics.world``：**初始**对象表。"""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_mechanics.world 必须是数组")
    if len(raw) > MAX_WORLD_OBJECTS:
        raise ValueError(f"custom_mechanics.world 最多 {MAX_WORLD_OBJECTS} 项")
    specs: list[WorldObjectSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"custom_mechanics.world[{index}] 必须是对象")
        object_id = _require_id(item.get("id"), field=f"world[{index}].id")
        if object_id in seen:
            raise ValueError(f"世界对象 id 重复: {object_id}")
        seen.add(object_id)
        kind = str(item.get("kind") or "").strip()
        if not kind:
            raise ValueError(f"world[{index}].kind 不能为空")
        state_raw = item.get("state")
        if state_raw is not None and not isinstance(state_raw, dict):
            raise ValueError(f"world[{index}].state 必须是对象")
        presentation_raw = item.get("presentation")
        if presentation_raw is not None and not isinstance(presentation_raw, dict):
            raise ValueError(f"world[{index}].presentation 必须是对象")
        state = dict(state_raw or {})
        presentation = dict(presentation_raw or {})
        overlap = sorted(set(state) & set(presentation))
        if overlap:
            raise ValueError(
                f"world[{index}] 的 presentation 与 state 出现同名键 {overlap} —— "
                "权威值与呈现必须分开，否则叙事会污染事实"
            )
        tags_raw = item.get("tags")
        if tags_raw is not None and not isinstance(tags_raw, list):
            raise ValueError(f"world[{index}].tags 必须是数组")
        specs.append(WorldObjectSpec(
            id=object_id,
            kind=kind[:32],
            state=state,
            tags=tuple(
                str(tag).strip()[:32] for tag in (tags_raw or ()) if str(tag).strip()
            ),
            presentation=presentation,
        ))
    return tuple(specs)


def _parse_world_effects(
    raw: Any, *, checks: tuple[CheckSpec, ...], world: tuple[WorldObjectSpec, ...],
) -> tuple[WorldEffectSpec, ...]:
    """解析 ``custom_mechanics.world_effects``：检定结果 → 世界变更。"""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_mechanics.world_effects 必须是数组")
    if len(raw) > MAX_WORLD_EFFECTS:
        raise ValueError(f"custom_mechanics.world_effects 最多 {MAX_WORLD_EFFECTS} 项")
    check_ids = {c.id for c in checks}
    degree_ids = {d.id for c in checks for d in c.degrees}
    object_ids = {o.id for o in world}
    specs: list[WorldEffectSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"custom_mechanics.world_effects[{index}] 必须是对象")
        where = f"world_effects[{index}]"
        check_id = _require_id(item.get("check"), field=f"{where}.check")
        if check_id not in check_ids:
            raise ValueError(f"{where}.check 引用了未声明的检定: {check_id}")
        degree_id = _require_id(item.get("degree"), field=f"{where}.degree")
        if degree_id not in degree_ids:
            raise ValueError(f"{where}.degree 引用了未声明的成功度: {degree_id}")
        target = _require_id(item.get("target"), field=f"{where}.target")
        if target not in object_ids:
            raise ValueError(f"{where}.target 引用了未声明的世界对象: {target}")
        path = str(item.get("field") or "").strip()
        if path == "tags" or (path.startswith("state.") and len(path) > len("state.")):
            field_path = path
        else:
            raise ValueError(
                f"{where}.field 只能是 tags 或 state.<路径>"
                f"（presentation 写不进去）: {path!r}"
            )
        op = str(item.get("op") or "").strip().lower()
        if op not in EFFECT_CHANGE_OPS:
            raise ValueError(
                f"{where}.op 必须是 {sorted(EFFECT_CHANGE_OPS)} 之一: {op!r}"
            )
        has_value = "value" in item
        delta = str(item.get("delta") or "").strip().lower()
        if path == "tags":
            if op not in ("add", "subtract"):
                raise ValueError(
                    f"{where}：集合字段 tags 只支持 add（授予）/ subtract（剥夺），收到 {op!r}"
                )
            if delta:
                raise ValueError(f"{where}：tags 不接受 delta（集合不能配骰式）")
        if op == "override":
            if not has_value or item.get("value") is None:
                raise ValueError(f"{where}.op=override 必须给出非 null 的 value")
            if delta:
                raise ValueError(f"{where}：override 不能同时给 delta")
        else:
            if has_value == bool(delta):
                raise ValueError(
                    f"{where}：op={op} 必须且只能给出 value 或 delta 之一"
                )
            if has_value and item.get("value") is None:
                raise ValueError(f"{where}：value 不能是 null")
            if delta and not re.fullmatch(r"[+-]?\d+(d\d+)?", delta):
                raise ValueError(
                    f"{where}.delta 必须是整数或骰式（可带符号）: {delta!r}"
                )
        family = str(item.get("intent_family") or "").strip().lower()
        if family:
            family = _require_id(family, field=f"{where}.intent_family")
        approach = ""
        raw_approach = str(item.get("approach_signature") or "").strip().lower()
        if raw_approach:
            approach = _require_id(raw_approach, field=f"{where}.approach_signature")
        policy = str(item.get("retry_policy") or "").strip().upper()
        if policy:
            try:
                policy = str(RetryPolicy(policy))
            except ValueError as exc:
                raise ValueError(
                    f"{where}.retry_policy 必须是 "
                    f"{[str(p) for p in RetryPolicy]} 之一: {policy!r}"
                ) from exc
        if bool(family) != bool(policy):
            # 不设默许值：默许成 FREE 意味着"失败不留痕"，会让检定彻底失去意义；
            # 默许成别的又会在背后改变语义。所以要求规则作者明确表态。
            raise ValueError(
                f"{where}：intent_family 与 retry_policy 必须同时给出"
                f"（FREE 会令失败不留痕、检定失去意义，所以不设默许值）"
            )
        specs.append(WorldEffectSpec(
            check=check_id, degree=degree_id, target=target, field=field_path,
            op=op, value=item.get("value") if has_value else None, delta=delta,
            intent_family=family, approach_signature=approach, retry_policy=policy,
        ))
    return tuple(specs)


def _parse_character_effects(
    raw: Any, *, checks: tuple[CheckSpec, ...],
) -> tuple[CharacterEffectSpec, ...]:
    """解析 ``custom_mechanics.character_effects``：检定结果 → 角色活跃效果。

    只声明"挂什么效果"，不声明"挂给谁" —— 效果永远落在行动者自己身上。
    跨角色效果需要一个选目标的交互（TODO），现在假装支持会写进规则书里再
    发现做不到。
    """

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_mechanics.character_effects 必须是数组")
    check_ids = {c.id for c in checks}
    degree_ids = {d.id for c in checks for d in c.degrees}
    specs: list[CharacterEffectSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"custom_mechanics.character_effects[{index}] 必须是对象")
        where = f"character_effects[{index}]"
        check_id = _require_id(item.get("check"), field=f"{where}.check")
        if check_id not in check_ids:
            raise ValueError(f"{where}.check 引用了未声明的检定: {check_id}")
        degree_id = _require_id(item.get("degree"), field=f"{where}.degree")
        if degree_id not in degree_ids:
            raise ValueError(f"{where}.degree 引用了未声明的成功度: {degree_id}")
        effect_id = _require_id(item.get("id"), field=f"{where}.id")
        raw_changes = item.get("changes")
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ValueError(f"{where}.changes 必须是非空数组")
        changes: list[tuple[str, str, Any]] = []
        for change_index, change in enumerate(raw_changes):
            cw = f"{where}.changes[{change_index}]"
            if not isinstance(change, dict):
                raise ValueError(f"{cw} 必须是对象")
            key = str(change.get("key") or "").strip()
            if not key:
                raise ValueError(f"{cw}.key 不能为空")
            if key.split(".")[0] in _EFFECT_FORBIDDEN_PREFIXES:
                raise ValueError(f"{cw}.key 写不进 presentation：{key!r}")
            op = str(change.get("op") or "").strip().lower()
            if op not in EFFECT_CHANGE_OPS:
                raise ValueError(
                    f"{cw}.op 必须是 {sorted(EFFECT_CHANGE_OPS)} 之一: {op!r}"
                )
            if "value" not in change:
                raise ValueError(f"{cw}.value 必须给出（角色效果不接受骰式 delta）")
            changes.append((key, op, change.get("value")))
        duration: dict[str, Any] | None = None
        raw_duration = item.get("duration")
        if raw_duration is not None:
            if not isinstance(raw_duration, dict):
                raise ValueError(f"{where}.duration 必须是对象")
            kind = str(raw_duration.get("type") or "").strip().lower()
            if kind not in DURATION_KINDS:
                raise ValueError(
                    f"{where}.duration.type 必须是 {list(DURATION_KINDS)} 之一: {kind!r}"
                )
            remaining = raw_duration.get("remaining", 1)
            if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
                raise ValueError(f"{where}.duration.remaining 必须是非负整数")
            duration = {"type": kind, "remaining": remaining}
        specs.append(CharacterEffectSpec(
            check=check_id, degree=degree_id, id=effect_id,
            label=str(item.get("label") or "")[:64],
            changes=tuple(changes), duration=duration,
        ))
    return tuple(specs)


def _parse_consumptions(
    raw: Any, *, checks: tuple[CheckSpec, ...], resources: tuple[ResourceSpec, ...],
) -> tuple[ConsumptionSpec, ...]:
    """解析 ``custom_mechanics.consumptions``：检定 → 代价。"""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("custom_mechanics.consumptions 必须是数组")
    check_ids = {c.id for c in checks}
    checks_by_id = {c.id: c for c in checks}
    resource_ids = {r.id for r in resources}
    specs: list[ConsumptionSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"custom_mechanics.consumptions[{index}] 必须是对象")
        where = f"consumptions[{index}]"
        check_id = _require_id(item.get("check"), field=f"{where}.check")
        if check_id not in check_ids:
            raise ValueError(f"{where}.check 引用了未声明的检定: {check_id}")
        resource_id = _require_id(item.get("resource"), field=f"{where}.resource")
        if resource_id not in resource_ids:
            raise ValueError(f"{where}.resource 引用了未声明的资源: {resource_id}")
        amount = item.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            # 负数消耗其实是奖励 —— 那是 effects 的活。混在一起会让
            # "资源不足" 这个判断失去意义。
            raise ValueError(f"{where}.amount 必须是正整数（奖励请用 effects）: {amount!r}")
        timing = str(item.get("timing") or "").strip().upper()
        try:
            timing = str(ConsumptionTiming(timing))
        except ValueError as exc:
            raise ValueError(
                f"{where}.timing 必须是 {[str(t) for t in ConsumptionTiming]} 之一: {timing!r}"
            ) from exc
        specs.append(ConsumptionSpec(
            check=check_id, resource=resource_id, amount=amount,
            timing=timing, label=str(item.get("label") or "")[:32],
        ))
        if timing != str(ConsumptionTiming.ON_DECLARE) and not checks_by_id[check_id].can_ever_roll:
            # 死声明：这个检定骰前就会无条件短路，成功度永远不会存在，
            # 所以 ON_RESOLVE / ON_SUCCESS 永远不会触发。与其让规则作者在
            # 跑团时发现"我写的消耗从来没扣过"，不如加载时就拒。
            raise ValueError(
                f"{where}：检定 {check_id} 声明了 can_succeed/can_fail/failure_matters "
                f"之一为 false，会无条件短路、永远不掷骰，所以 {timing} 永远不会触发"
                f"（要先付再说，请用 ON_DECLARE）"
            )
    return tuple(specs)


def parse_custom_mechanics(template: Any) -> CustomMechanics:
    """从规则模板解析 ``custom_mechanics``；未声明时返回空声明。"""

    if not isinstance(template, dict):
        return EMPTY
    raw = template.get("custom_mechanics")
    if raw is None:
        return EMPTY
    if not isinstance(raw, dict):
        raise ValueError("custom_mechanics 必须是对象")
    resources = _parse_resources(raw.get("resources"))
    checks = _parse_checks(raw.get("checks"))
    effects = _parse_effects(raw.get("effects"), checks=checks, resources=resources)
    world = _parse_world(raw.get("world"))
    world_effects = _parse_world_effects(
        raw.get("world_effects"), checks=checks, world=world,
    )
    character_effects = _parse_character_effects(
        raw.get("character_effects"), checks=checks,
    )
    consumptions = _parse_consumptions(
        raw.get("consumptions"), checks=checks, resources=resources,
    )
    raw_fields = raw.get("authoritative_fields")
    authoritative_fields: tuple[str, ...] = ()
    if raw_fields is not None:
        if not isinstance(raw_fields, list):
            raise ValueError("custom_mechanics.authoritative_fields 必须是数组")
        authoritative_fields = tuple(
            str(item).strip() for item in raw_fields if str(item).strip()
        )
    return CustomMechanics(
        resources=resources, checks=checks, effects=effects,
        authoritative_fields=authoritative_fields,
        world=world, world_effects=world_effects,
        character_effects=character_effects,
        consumptions=consumptions,
    )


__all__ = [
    "EMPTY",
    "RESOURCE_PREFIX",
    "CheckSpec",
    "CustomMechanics",
    "DegreeSpec",
    "EffectSpec",
    "ResourceSpec",
    "ValueRef",
    "WorldEffectSpec",
    "WorldObjectSpec",
    "parse_custom_mechanics",
]
