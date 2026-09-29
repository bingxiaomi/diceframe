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

from src.rulesets.adjudication import EFFECT_CHANGE_OPS
from src.rulesets.attempts import RetryPolicy

MAX_RESOURCES = 24
MAX_CHECKS = 48
MAX_DEGREES = 8
MAX_EFFECTS = 64
MAX_WORLD_OBJECTS = 32
MAX_WORLD_EFFECTS = 64

#: 世界效果允许的字段根前缀。``presentation`` 刻意不在其中。
_WORLD_FIELD_ROOTS = ("state.", "tags")

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
    """成功度分级。``fallback=True`` 的档位必须且只能有一个，且放在最后。"""

    id: str
    label: str
    max_ratio: float
    fallback: bool


@dataclass(frozen=True, slots=True)
class CheckSpec:
    id: str
    name: str
    dice: str
    comparison: str
    target: ValueRef
    degrees: tuple[DegreeSpec, ...]


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
class CustomMechanics:
    resources: tuple[ResourceSpec, ...]
    checks: tuple[CheckSpec, ...]
    effects: tuple[EffectSpec, ...]
    authoritative_fields: tuple[str, ...]
    world: tuple[WorldObjectSpec, ...] = ()
    world_effects: tuple[WorldEffectSpec, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.resources or self.checks or self.effects)

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
        ))
    fallbacks = [d for d in degrees if d.fallback]
    if len(fallbacks) != 1:
        raise ValueError(f"{field} 必须且只能有一个 fallback: true 档位")
    if not degrees[-1].fallback:
        raise ValueError(f"{field} 的 fallback 档位必须放在最后")
    ratios = [d.max_ratio for d in degrees if not d.fallback]
    if ratios != sorted(ratios):
        raise ValueError(f"{field} 的 max_ratio 必须从小到大排列")
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
        ))
    return tuple(specs)


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
