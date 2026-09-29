"""活跃效果（Active Effects）：基础值 + 效果 = 有效值。

**效果永远不直接改基础值。** 把 ``bless`` 的 ``+1d4`` 写进 ``abilities.wis``，
以后就再也分不清哪部分是角色底子、哪部分是一次性加值 —— 答不出"这个 +2
哪来的、什么时候到期、另一个 debuff 怎么叠"。

::

    基础值（权威，只读）
      + 活跃效果列表（有来源、有来源时刻、有剩余时长）
      = 有效值（每次读取时算出来，**不落盘**）

"不改基础值"不是文档约定，而是 :func:`project` 的结构性质：它只读 ``base``，
返回一份新对象，从不回写。

## 幂等按 id，不是叠加

同一个 ``id`` 再次施加是**续期/替换**，不是叠一层。否则同一个 buff 被描述
两次就会变成 +2d4，而且再也查不出哪来的。想叠就换 id。

## 时长

``{"type": "round"|"turn"|"rest"|"scene", "remaining": int}``。
:func:`advance_durations` 是纯函数：按类型递减、归零即过期并返回给调用方
（丢弃还是归档由调用方决定）。

算子语义与 :mod:`src.rulesets.world` **共用** :func:`adjudication.compute_change`
—— 两处各写一遍必然漂移。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.rulesets.adjudication import EFFECT_CHANGE_OPS, AdjudicationError, compute_change
from src.rulesets.world import read_field, write_field

__all__ = [
    "DURATION_KINDS",
    "MAX_EFFECTS_PER_ACTOR",
    "ActiveEffect",
    "Change",
    "EffectError",
    "advance_durations",
    "effects_for",
    "load_effects",
    "project",
    "remove_effect",
    "upsert_effect",
]

#: 时长的计时单位。``None``/缺失 = 永久（直到被显式移除）。
DURATION_KINDS = ("round", "turn", "rest", "scene")

MAX_EFFECTS_PER_ACTOR = 32


class EffectError(ValueError):
    """活跃效果数据不合法。``ValueError`` 子类，宿主服务层已按它处理。"""


@dataclass(frozen=True, slots=True)
class Change:
    """一条修正量：往角色的哪个路径、用什么算子、加多少。"""

    key: str
    op: str
    value: Any

    def validate(self) -> None:
        if not str(self.key or "").strip():
            raise EffectError("修正量缺少 key（点分路径，例如 derived.armor_class）")
        if self.op not in EFFECT_CHANGE_OPS:
            raise EffectError(
                f"修正量 {self.key} 的算子 {self.op!r} 未知"
                f"（合法值：{', '.join(EFFECT_CHANGE_OPS)}）"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "op": self.op, "value": self.value}


@dataclass(frozen=True, slots=True)
class ActiveEffect:
    """一个仍在生效的效果。

    :param id: 效果身份。同 id 重复施加是**续期**，不是叠加。
    :param source: 溯源（哪个法术/物品/意图产生的）。
    :param duration: ``None`` = 永久。
    """

    id: str
    target_uid: str
    changes: tuple[Change, ...]
    source: str = ""
    duration: dict[str, Any] | None = None
    note: str = ""

    def validate(self) -> None:
        if not str(self.id or "").strip():
            raise EffectError("效果缺少 id")
        if not str(self.target_uid or "").strip():
            raise EffectError(f"效果 {self.id} 缺少 target_uid")
        if not self.changes:
            raise EffectError(f"效果 {self.id} 没有任何修正量")
        for change in self.changes:
            change.validate()
        if self.duration is not None:
            if not isinstance(self.duration, dict):
                raise EffectError(f"效果 {self.id} 的 duration 必须是对象或 null")
            kind = str(self.duration.get("type") or "")
            if kind not in DURATION_KINDS:
                raise EffectError(
                    f"效果 {self.id} 的时长类型 {kind!r} 未知"
                    f"（合法值：{', '.join(DURATION_KINDS)}）"
                )
            if int(self.duration.get("remaining") or 0) < 0:
                raise EffectError(f"效果 {self.id} 的 remaining 不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target_uid": self.target_uid,
            "source": self.source,
            "changes": [change.to_dict() for change in self.changes],
            "duration": deepcopy(self.duration),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 账本读写：ruleset_state["effects"] = {uid: [effect, ...]}
# ---------------------------------------------------------------------------
def load_effects(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """把 ``ruleset_state["effects"]`` 读成 ``{uid: [效果]}``（带校验）。"""

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise EffectError("effects 必须是 {uid: [效果]} 形式的对象")
    ledger: dict[str, list[dict[str, Any]]] = {}
    for uid, items in raw.items():
        if not isinstance(items, list):
            raise EffectError(f"effects[{uid}] 必须是数组")
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise EffectError(f"effects[{uid}][{index}] 必须是对象")
        ledger[str(uid)] = [dict(item) for item in items]
    return ledger


def effects_for(ledger: Mapping[str, Any], uid: str) -> list[dict[str, Any]]:
    items = ledger.get(str(uid or ""))
    return [dict(item) for item in items] if isinstance(items, list) else []


def upsert_effect(ledger: dict[str, list[dict[str, Any]]], effect: ActiveEffect) -> dict[str, Any]:
    """写入一个效果：同 ``(uid, id)`` 是**续期/替换**，不是叠一层。

    返回写入的那条。超出 :data:`MAX_EFFECTS_PER_ACTOR` 时丢最旧的。
    """

    effect.validate()
    entry = effect.to_dict()
    uid = str(effect.target_uid)
    bucket = ledger.setdefault(uid, [])
    if not isinstance(bucket, list):
        raise EffectError(f"effects[{uid}] 必须是数组")
    for index, existing in enumerate(bucket):
        if isinstance(existing, Mapping) and str(existing.get("id") or "") == effect.id:
            bucket[index] = entry
            return entry
    bucket.append(entry)
    if len(bucket) > MAX_EFFECTS_PER_ACTOR:
        del bucket[: len(bucket) - MAX_EFFECTS_PER_ACTOR]
    return entry


def remove_effect(ledger: dict[str, list[dict[str, Any]]], uid: str, effect_id: str) -> bool:
    """移除一个效果。返回是否真的移除了。"""

    bucket = ledger.get(str(uid or ""))
    if not isinstance(bucket, list):
        return False
    for index, existing in enumerate(bucket):
        if isinstance(existing, Mapping) and str(existing.get("id") or "") == effect_id:
            del bucket[index]
            return True
    return False


def advance_durations(
    ledger: dict[str, list[dict[str, Any]]], kind: str,
) -> list[dict[str, Any]]:
    """按计时单位推进一步（原地修改），返回**过期**的效果。

    ``kind`` 取 ``round`` / ``turn`` / ``rest`` / ``scene``：匹配类型的效果
    递减 1，归零即过期并移出账本；``rest`` / ``scene`` 一次性清空对应类型
    （不递减，因为休息不是按次数计的）。
    """

    if kind not in DURATION_KINDS:
        raise EffectError(
            f"未知的计时单位 {kind!r}（合法值：{', '.join(DURATION_KINDS)}）"
        )
    one_shot = kind in ("rest", "scene")
    expired: list[dict[str, Any]] = []
    for uid, bucket in list(ledger.items()):
        if not isinstance(bucket, list):
            continue
        kept: list[dict[str, Any]] = []
        for item in bucket:
            duration = item.get("duration") if isinstance(item, Mapping) else None
            if not isinstance(duration, Mapping) or str(duration.get("type") or "") != kind:
                kept.append(item)
                continue
            if one_shot:
                expired.append(dict(item))
                continue
            remaining = int(duration.get("remaining") or 0) - 1
            if remaining <= 0:
                expired.append(dict(item))
                continue
            updated = dict(item)
            updated["duration"] = {**duration, "remaining": remaining}
            kept.append(updated)
        ledger[uid] = kept
    return expired


# ---------------------------------------------------------------------------
# 投影：基础值 + 效果 = 有效值
# ---------------------------------------------------------------------------
def project(base: Mapping[str, Any], effects: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """算出有效值。**``base`` 不会被修改** —— 这是本模块的核心保证。

    效果按给定顺序应用，所以后一个能看到前一个的结果（``override`` 会盖掉
    先前的 ``add``，反过来也一样 —— 顺序是有意义的）。
    """

    effective = deepcopy(dict(base))
    for effect in effects:
        changes = effect.get("changes") if isinstance(effect, Mapping) else None
        if not isinstance(changes, (list, tuple)):
            raise EffectError("效果缺少 changes 数组")
        where = str(effect.get("id") or "?")
        for change in changes:
            if not isinstance(change, Mapping):
                raise EffectError(f"效果 {where} 的 changes 里混入了非对象元素")
            _apply_change(effective, change, effect_id=where)
    return effective


def _apply_change(
    effective: dict[str, Any], change: Mapping[str, Any], *, effect_id: str,
) -> None:
    key = str(change.get("key") or "").strip()
    op = str(change.get("op") or "")
    if not key:
        raise EffectError(f"效果 {effect_id} 的修正量缺少 key")
    if key.startswith("presentation"):
        raise EffectError(
            f"效果 {effect_id} 不允许写 {key} —— 呈现只能由权威值派生"
        )
    current = read_field(effective, key)
    try:
        updated, _changed = compute_change(
            current, op, change.get("value"), where=f"效果 {effect_id} 的 {key}",
        )
    except AdjudicationError as exc:
        raise EffectError(str(exc)) from exc
    write_field(effective, key, updated)
