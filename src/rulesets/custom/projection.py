"""只读投影：给前端（``gameplay_view``）和给 GM 模型（``build_llm_view``）。

两条硬约束：

1. ``gameplay_view`` 必须遵守观看者可见性 —— 非 GM 只看得到自己席位的资源
   细节，其他人的只给公开摘要。这是 DiceFrame 的隐私边界（见
   ``src/engine/visibility_rules.py`` 的同族设计），不要把 GM 视图直接透给玩家。
2. ``build_llm_view`` 必须以 ``project_legacy_game_context`` 为底，再追加
   ``ruleset_authority`` 段。丢掉 legacy 上下文会让 GM 失忆。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.engine.legacy_game_projection import project_legacy_game_context
from src.rulesets.custom.binding import RUNTIME_ID
from src.rulesets.custom.manifest import CustomMechanics
from src.rulesets.custom.state import (
    read_state,
    resource_cap,
    sheet_attributes,
    sheet_special_stats,
)

# 给 GM 模型的权威段落声明：只叙述已结算结果，不许改写机制。
LLM_POLICY = "Narrate resolved results only; never invent or mutate custom mechanics."


def _player_name(instance: Any, uid: str) -> str:
    players = getattr(instance, "players", {}) or {}
    entry = players.get(uid) if isinstance(players, dict) else None
    if isinstance(entry, dict):
        name = str(entry.get("character_name") or entry.get("name") or "").strip()
        if name:
            return name
    return str(uid)


def _resources_with_caps(
    instance: Any, mechanics: CustomMechanics, uid: str,
    resources: dict[str, int],
) -> dict[str, Any]:
    sheet = instance.get_character_sheet(uid) if hasattr(instance, "get_character_sheet") else {}
    resource_ids = {spec.id for spec in mechanics.resources}
    attributes = sheet_attributes(sheet)
    special_stats = sheet_special_stats(sheet, resource_ids)
    out: dict[str, Any] = {}
    for spec in mechanics.resources:
        value = resources.get(spec.id)
        if value is None:
            continue
        projected: dict[str, Any] = {
            "id": spec.id, "name": spec.name, "value": int(value),
        }
        cap = resource_cap(spec, attributes, special_stats, resources)
        if cap is not None:
            projected["max"] = cap
        out[spec.id] = projected
    return out


def gameplay_view(
    instance: Any, mechanics: CustomMechanics, *,
    viewer_id: str = "", viewer_is_gm: bool = False,
) -> dict[str, Any]:
    """前端只读投影。非 GM 只看到自己席位的完整资源，其他人只给公开摘要。"""

    state = read_state(instance)
    players = state.get("players") or {}
    viewer = str(viewer_id or "")

    seats: list[dict[str, Any]] = []
    for uid, data in players.items():
        if not isinstance(data, dict):
            continue
        resources = data.get("resources") or {}
        if not isinstance(resources, dict):
            resources = {}
        seat: dict[str, Any] = {
            "player_id": uid,
            "name": _player_name(instance, uid),
            "is_self": uid == viewer,
        }
        if viewer_is_gm or uid == viewer:
            seat["resources"] = _resources_with_caps(instance, mechanics, uid, resources)
        else:
            # 公开摘要：只暴露是否存在，不给具体数值，避免跨席位信息泄漏。
            seat["resource_count"] = len(resources)
        seats.append(seat)

    return {
        "runtime_id": RUNTIME_ID,
        "state_version": int(state.get("revision", 0) or 0),
        "seats": seats,
        "counters": deepcopy(state.get("counters") or {}) if viewer_is_gm else {},
        "flags": deepcopy(state.get("flags") or {}) if viewer_is_gm else {},
        "declared_checks": [
            {"id": check.id, "name": check.name, "dice": check.dice}
            for check in mechanics.checks
        ],
    }


def build_llm_view(
    instance: Any, mechanics: CustomMechanics, *, latest_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GM 上下文 = legacy 投影 + 本运行时的权威段落。"""

    context = project_legacy_game_context(instance)
    state = read_state(instance)
    players = state.get("players") or {}
    authority_seats: dict[str, Any] = {}
    for uid, data in players.items():
        if not isinstance(data, dict):
            continue
        resources = data.get("resources") or {}
        if not isinstance(resources, dict):
            continue
        authority_seats[uid] = {
            "name": _player_name(instance, uid),
            "resources": _resources_with_caps(instance, mechanics, uid, resources),
        }
    context["ruleset_authority"] = {
        "runtime_id": RUNTIME_ID,
        "state_version": int(state.get("revision", 0) or 0),
        "seats": authority_seats,
        "counters": deepcopy(state.get("counters") or {}),
        "flags": deepcopy(state.get("flags") or {}),
        "declared_checks": [
            {
                "id": check.id, "name": check.name, "dice": check.dice,
                "comparison": check.comparison,
                "degrees": [{"id": d.id, "label": d.label} for d in check.degrees],
            }
            for check in mechanics.checks
        ],
        "latest_event": deepcopy(latest_event) if latest_event else None,
        "policy": LLM_POLICY,
    }
    return context


__all__ = ["LLM_POLICY", "build_llm_view", "gameplay_view"]
