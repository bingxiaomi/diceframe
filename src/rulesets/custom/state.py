"""本运行时的权威状态容器。

状态存在 ``GameInstance.ruleset_state``（最终落到
``modules["ruleset_runtime"]["state"]``）里，因此自动获得：

- 存档 encode/decode；
- reset / 重开时重建 state（binding 保留）；
- 事务快照回滚（``restore_from_transaction``）。

注意：这里刻意**不**导入 ``src.engine.modules``（它的包级 ``__init__`` 会拉入
lorebook 存储并形成导入环）。D&D 2024 运行时同样只通过
``instance.ruleset_state`` / ``instance.event_ledger`` 属性读写，本模块沿用
同一契约。

状态形状::

    {
      "state_schema_version": 1,
      "revision": 0,
      "players": {"<uid>": {"resources": {"sanity": 42}}},
      "counters": {},
      "flags": {}
    }

读操作用 ``read_state``（归一化但不落盘），写操作用 ``write_state``（整块替换，
配 ``revision`` 自增，便于前端与 LLM 上下文判断新鲜度）。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.rulesets.custom.binding import MAX_EVENT_LEDGER, STATE_SCHEMA_VERSION
from src.rulesets.custom.manifest import CustomMechanics, ResourceSpec


def _fresh() -> dict[str, Any]:
    return {
        "state_schema_version": STATE_SCHEMA_VERSION,
        "revision": 0,
        "players": {},
        "counters": {},
        "flags": {},
    }


def read_state(instance: Any) -> dict[str, Any]:
    """读取并归一化状态；不修改 instance。"""

    raw = getattr(instance, "ruleset_state", None)
    if not isinstance(raw, dict):
        return _fresh()
    if raw.get("state_schema_version") != STATE_SCHEMA_VERSION:
        # 未知版本保持不透明：交给 migrate_state 处理，不在这里猜。
        return raw
    normalized = dict(raw)
    normalized["revision"] = int(raw.get("revision", 0) or 0)
    for key in ("players", "counters", "flags"):
        if not isinstance(normalized.get(key), dict):
            normalized[key] = {}
    return normalized


def write_state(instance: Any, state: dict[str, Any]) -> dict[str, Any]:
    """整块替换状态并自增 revision，返回写入后的副本。"""

    payload = deepcopy(state)
    payload["state_schema_version"] = STATE_SCHEMA_VERSION
    payload["revision"] = int(payload.get("revision", 0) or 0) + 1
    instance.ruleset_state = payload
    return payload


def append_event(instance: Any, event: dict[str, Any]) -> None:
    """追加一条权威事件到 ledger（有界保留，供诊断与回放）。"""

    ledger = getattr(instance, "event_ledger", None)
    if not isinstance(ledger, list):
        return
    ledger.append(deepcopy(event))
    if len(ledger) > MAX_EVENT_LEDGER:
        del ledger[: len(ledger) - MAX_EVENT_LEDGER]


def _int_map(raw: Any) -> dict[str, int]:
    """把属性/特殊值的多种历史形状归一为 {key: int}。

    legacy 角色卡的 ``attributes`` 是 dict；``special_stats`` 可能是 dict，
    也可能被摊平成顶层键（如 ``"sanity": 55``）。这里两种都接受。
    """

    if isinstance(raw, dict):
        out: dict[str, int] = {}
        for key, value in raw.items():
            try:
                out[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, dict):
                key = str(item.get("key") or "").strip()
                if not key:
                    continue
                try:
                    out[key] = int(item.get("value", 0) or 0)
                except (TypeError, ValueError):
                    continue
        return out
    return {}


def sheet_attributes(sheet: Any) -> dict[str, int]:
    if not isinstance(sheet, dict):
        return {}
    return _int_map(sheet.get("attributes"))


def sheet_special_stats(sheet: Any, resource_ids: set[str]) -> dict[str, int]:
    """特殊值来源：显式 ``special_stats``，以及没有被资源占用、但出现在
    角色卡顶层的数值键（例如 legacy 卡把 ``sanity`` 直接写在顶层）。"""

    if not isinstance(sheet, dict):
        return {}
    out = _int_map(sheet.get("special_stats"))
    for key, value in sheet.items():
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if key in resource_ids or key.startswith("max_"):
            continue
        out.setdefault(str(key), value)
    return out


def derive_resources(
    mechanics: CustomMechanics,
    sheet: Any,
    *,
    existing: dict[str, int] | None = None,
) -> dict[str, int]:
    """按声明派生资源初始值/上限。

    已有值优先保留（``existing``），避免刷新角色卡时把跑团中消耗掉的资源重置。
    """

    attributes = sheet_attributes(sheet)
    resource_ids = {spec.id for spec in mechanics.resources}
    special_stats = sheet_special_stats(sheet, resource_ids)
    current = dict(existing or {})
    derived: dict[str, int] = {}
    for spec in mechanics.resources:
        derived[spec.id] = _derive_one(spec, attributes, special_stats, current)
    return derived


def _derive_one(
    spec: ResourceSpec,
    attributes: dict[str, int],
    special_stats: dict[str, int],
    current: dict[str, int],
) -> int:
    if spec.id in current:
        return int(current[spec.id])
    base = spec.initial.resolve(
        attributes=attributes, special_stats=special_stats, resources=current,
    )
    value = spec.default if base is None else base
    if spec.maximum is not None:
        cap = spec.maximum.resolve(
            attributes=attributes, special_stats=special_stats, resources=current,
        )
        if cap is not None:
            value = min(value, cap)
    return int(value)


def resource_cap(
    spec: ResourceSpec,
    attributes: dict[str, int],
    special_stats: dict[str, int],
    resources: dict[str, int],
) -> int | None:
    if spec.maximum is None:
        return None
    cap = spec.maximum.resolve(
        attributes=attributes, special_stats=special_stats, resources=resources,
    )
    return None if cap is None else int(cap)


def resolve_target(
    mechanics: CustomMechanics,
    sheet: Any,
    *,
    ref: Any,
    resources: dict[str, int],
    fallback: int = 50,
) -> int:
    """把 ``target`` 声明求值成具体数字。引用缺失时用 ``fallback``。

    注意：这里刻意不猜、不抛错 —— 玩家还没建卡时也要能显示检定按钮。
    """

    attributes = sheet_attributes(sheet)
    resource_ids = {spec.id for spec in mechanics.resources}
    special_stats = sheet_special_stats(sheet, resource_ids)
    value = ref.resolve(
        attributes=attributes, special_stats=special_stats, resources=resources,
    )
    return fallback if value is None else int(value)


__all__ = [
    "append_event",
    "derive_resources",
    "read_state",
    "resolve_target",
    "resource_cap",
    "sheet_attributes",
    "sheet_special_stats",
    "write_state",
]
