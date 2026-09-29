"""世界状态（World State）：裁定的权威承接层。

这一层回答"**裁定说该发生的，事实层真的发生了吗**"。

它刻意很薄 —— 只有四个概念，不做 ECS、不做场景图、不做类的继承体系：

    Object      一个可以被规则谈论的东西（门 / 箱子 / 守卫 / 机关）
    State       它的权威字段（``locked`` / ``open`` / ``broken``）
    Tags        粗粒度分类（``wooden`` / ``interactable``）
    Presentation 给人看的话（描述文本）

规则真正需要的是 ``world["door_01"].state["locked"]``，
**不是** Python 类型体系知道它是 ``OakDoor``。所以这里没有
``Door`` / ``Chest`` / ``Lever`` 之类几十个类。

## 权威与呈现的方向是单向的

::

    authoritative  →  presentation        允许
    presentation   →  authoritative       **禁止**

呈现层（``presentation``）是给叙事与 UI 的，规则**永远不读**它。
这条不是文档约定，而是代码保证：:func:`apply_effects` 只接受
``state.*`` 与 ``tags``，碰到 ``presentation.*`` 直接报错。

否则叙事层一句"门缓缓打开"就可能污染世界事实 —— 那正是要防的病。

## 版本号是防刷骰的支点

每次**实际发生变化**时 ``version`` 自增（没变就不增）。尝试记录绑定
``version`` 之后，GM 改世界状态会让旧尝试**自动失效**，不需要任何手工
``reset_attempts()``：

    GM 重新锁门 → version 8 → 绑定 version 7 的尝试自动不适用

注意一个容易写错的细节：尝试要记的是**自己这次落完之后的版本**，
不是尝试前的版本 —— 记"之前"会让失败本身改对象的那些尝试（``lock_jammed``）
把自己作废，从而变成可以无限重试。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from src.rulesets.adjudication import AdjudicationError, EffectDescriptor, compute_change

__all__ = [
    "PRESENTATION_PREFIX",
    "STATE_PREFIX",
    "TAGS_FIELD",
    "WORLD_TARGET_NOT_FOUND",
    "WorldError",
    "WorldObject",
    "apply_effect",
    "apply_effects",
    "dump_world",
    "load_world",
    "read_field",
    "world_object_id",
    "write_field",
]

STATE_PREFIX = "state"
TAGS_FIELD = "tags"
PRESENTATION_PREFIX = "presentation"

#: 找不到目标对象时的错误码。**不自动创建对象** —— 规则写错必须可见。
WORLD_TARGET_NOT_FOUND = "WORLD_TARGET_NOT_FOUND"


class WorldError(ValueError):
    """世界状态操作不合法。``ValueError`` 子类，宿主服务层已按它处理。"""


# ---------------------------------------------------------------------------
# 对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WorldObject:
    """一个可以被规则谈论的东西。

    :param state: 权威字段。**规则只读这里。**
    :param tags: 粗粒度分类。用集合语义增删（等价于 grant / deny）。
    :param presentation: 给人看的话。**规则永远不读**，只写不读是单向保护。
    :param version: 每次实际变化自增。防刷骰与失效判定都靠它。
    """

    id: str
    kind: str
    state: dict[str, Any] = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    presentation: dict[str, Any] = field(default_factory=dict)
    version: int = 0

    def validate(self) -> None:
        if not str(self.id or "").strip():
            raise WorldError("世界对象缺少 id")
        if not str(self.kind or "").strip():
            raise WorldError(f"世界对象 {self.id} 缺少 kind")
        if not isinstance(self.state, dict):
            raise WorldError(f"世界对象 {self.id} 的 state 必须是对象")
        if self.version < 0:
            raise WorldError(f"世界对象 {self.id} 的 version 不能为负")
        for key in self.presentation:
            if str(key) in self.state:
                raise WorldError(
                    f"世界对象 {self.id} 的 presentation 与 state 出现同名键 "
                    f"{key!r} —— 权威值与呈现必须分开，否则叙事会污染事实"
                )

    @classmethod
    def from_dict(cls, object_id: str, raw: Mapping[str, Any]) -> "WorldObject":
        if not isinstance(raw, Mapping):
            raise WorldError(f"世界对象 {object_id} 必须是对象")
        tags = raw.get("tags")
        obj = cls(
            id=str(object_id),
            kind=str(raw.get("kind") or ""),
            state=deepcopy(dict(raw.get("state") or {})),
            tags=frozenset(str(x) for x in (tags if isinstance(tags, (list, tuple, set, frozenset)) else ())),
            presentation=deepcopy(dict(raw.get("presentation") or {})),
            version=int(raw.get("version") or 0),
        )
        obj.validate()
        return obj

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": deepcopy(self.state),
            "tags": sorted(self.tags),
            "presentation": deepcopy(self.presentation),
            "version": self.version,
        }


def world_object_id(descriptor: EffectDescriptor) -> str:
    """效果描述符指向的对象 id。"""

    return str(descriptor.target or "").strip()


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_world(raw: Any) -> dict[str, WorldObject]:
    """把 ``ruleset_state["world"]`` 的原始 dict 读成对象表（带校验）。"""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise WorldError("world 必须是 {id: 对象} 形式的对象")
    return {str(key): WorldObject.from_dict(str(key), value) for key, value in raw.items()}


def dump_world(objects: Mapping[str, WorldObject]) -> dict[str, Any]:
    """写回 JSON 安全的表示（``tags`` 排序以保证落盘稳定）。"""

    return {str(key): obj.to_dict() for key, obj in objects.items()}


def read_field(target: Any, path: str) -> Any:
    """按点分路径读值。``state.locked`` / ``state.stats.hp``。

    读取辅助遇错就返回 ``None``（读不到就是读不到）；严格的路径校验只在
    写入路径（:func:`apply_effect`）上做。
    """

    text = str(path or "").strip()
    if not text:
        return None
    node = target
    for part in text.split("."):
        if not part:
            return None
        if isinstance(node, Mapping):
            if part not in node:
                return None
            node = node[part]
        else:
            return None
    return node


def _split_path(path: str) -> list[str]:
    text = str(path or "").strip()
    if not text:
        raise WorldError("效果缺少 field（点分路径，例如 state.locked）")
    return [part for part in text.split(".") if part]


# ---------------------------------------------------------------------------
# 应用效果（reducer 的原子单位）
# ---------------------------------------------------------------------------
def apply_effect(world: dict[str, Any], descriptor: EffectDescriptor) -> bool:
    """把一个效果落到 ``world``（原地修改）。返回**是否真的改变了值**。

    只有真正改变才返回 ``True``，调用方据此决定要不要自增 ``version`` ——
    「写了但值没变」不该让命中同一目标的尝试记录失效。

    ``descriptor.value`` 必须是**已解析的具体值**。公式（``"1d6"``）属于
    ``resolve_intent`` 的职责：那里才有注入的服务端 RNG。这里看到字符串公式
    会直接报错，而不是猜一个数。
    """

    descriptor.validate()
    object_id = world_object_id(descriptor)
    if object_id not in world:
        raise WorldError(
            f"{WORLD_TARGET_NOT_FOUND}: 世界里没有对象 {object_id!r} —— 不自动创建"
        )
    raw = world[object_id]
    if not isinstance(raw, dict):
        raise WorldError(f"世界对象 {object_id} 必须是对象")

    parts = _split_path(descriptor.field)
    root = parts[0]
    if root == PRESENTATION_PREFIX:
        raise WorldError(
            f"不允许通过效果写 {PRESENTATION_PREFIX}.* —— "
            "呈现层只能由权威状态派生，反向推导会污染世界事实"
        )
    if root not in (STATE_PREFIX, TAGS_FIELD):
        raise WorldError(
            f"效果的 field 必须以 {STATE_PREFIX}. 或 {TAGS_FIELD} 开头：{descriptor.field!r}"
        )

    if root == TAGS_FIELD:
        if len(parts) != 1:
            raise WorldError("tags 不支持子路径")
        return _apply_tags(raw, descriptor)

    path = parts[1:]
    if not path:
        raise WorldError("state 路径不能为空")
    current = read_field(raw.get("state") or {}, ".".join(path))
    updated, changed = _compute(current, descriptor, object_id)
    if not changed:
        return False
    _write(raw.setdefault("state", {}), path, updated)
    return True


def _apply_tags(raw: dict[str, Any], descriptor: EffectDescriptor) -> bool:
    tags = {str(x) for x in (raw.get("tags") or ()) if str(x)}
    op = descriptor.op
    value = descriptor.value
    incoming = {str(x) for x in value} if isinstance(value, (list, tuple, set, frozenset)) else {str(value)}
    incoming.discard("")
    if op == "add":
        merged = tags | incoming
    elif op == "subtract":
        merged = tags - incoming
    else:
        raise WorldError(
            f"集合字段 tags 只支持 add（授予）/ subtract（剥夺），收到 {op!r}"
        )
    if merged == tags:
        return False
    raw["tags"] = sorted(merged)
    return True


def _compute(
    current: Any, descriptor: EffectDescriptor, object_id: str,
) -> tuple[Any, bool]:
    """算新值。算子语义在 :func:`adjudication.compute_change`（唯一实现）。

    这里只负责把错误类型换回本层的 :class:`WorldError`，让调用方的
    ``except WorldError`` 仍然有效。
    """

    try:
        return compute_change(
            current, descriptor.op, descriptor.value,
            where=f"{object_id}.{descriptor.field}",
        )
    except AdjudicationError as exc:
        raise WorldError(str(exc)) from exc


def write_field(container: dict[str, Any], path: str, value: Any) -> None:
    """按点分路径写值（中间层不存在则创建）。

    公开给 :mod:`src.rulesets.effects` 复用 —— 路径语义只能有一份实现。
    """

    _write(container, _split_path(path), value)


def _write(container: dict[str, Any], path: list[str], value: Any) -> None:
    node = container
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def apply_effects(
    world: dict[str, Any], effects: Iterable[EffectDescriptor],
) -> list[str]:
    """按顺序应用一批效果（原地修改），返回**被改动的对象 id**。

    任一效果失败就抛 :class:`WorldError`。**整个 EventBatch 的原子性由调用方
    （``GameInstance.restore_ruleset_transaction``）负责回滚** —— 这里不自己
    做一半回滚，避免出现两套回滚语义。

    顺序是有意义的：同一批里后一个效果能看到前一个的结果。
    """

    touched: list[str] = []
    for descriptor in effects:
        if not isinstance(descriptor, EffectDescriptor):
            raise WorldError("效果列表里混入了非效果描述符")
        changed = apply_effect(world, descriptor)
        if not changed:
            continue
        object_id = world_object_id(descriptor)
        raw = world[object_id]
        raw["version"] = int(raw.get("version") or 0) + 1
        if object_id not in touched:
            touched.append(object_id)
    return touched
