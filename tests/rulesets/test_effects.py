"""活跃效果层（``src/rulesets/effects.py``）的单元测试。

纯数据 + 纯函数。重点验四件事：

1. **base 永不被修改** —— 这是"不要直接改基础数值"的结构保证；
2. **算子语义只有一份实现** —— 与 ``world`` 共用 ``compute_change``；
3. **同 id 是续期不是叠加** —— 否则同一个 buff 被描述两次就变成 +2d4；
4. **时长按单位递减，归零即过期** —— 休息/场景是一次性清空。
"""

from __future__ import annotations

import pytest

from src.rulesets.effects import (
    MAX_EFFECTS_PER_ACTOR,
    ActiveEffect,
    Change,
    EffectError,
    advance_durations,
    effects_for,
    load_effects,
    project,
    remove_effect,
    upsert_effect,
)

UID = "player:pc_1"


def _base() -> dict:
    return {
        "abilities": {"dex": 16, "wis": 12},
        "derived": {"armor_class": 15, "saving_throw": 3},
        "conditions": [],
    }


def _shield() -> dict:
    return {
        "id": "shield_of_faith",
        "target_uid": UID,
        "source": "spell:shield_of_faith",
        "changes": [{"key": "derived.armor_class", "op": "add", "value": 2}],
        "duration": {"type": "round", "remaining": 8},
        "note": "",
    }


# ---------------------------------------------------------------------------
# 1. 核心保证：base 不被修改
# ---------------------------------------------------------------------------
def test_project_never_mutates_base() -> None:
    """把 buff 写进基础值就再也分不清底子和一次性加值了。"""

    base = _base()
    snapshot = {
        "abilities": dict(base["abilities"]),
        "derived": dict(base["derived"]),
        "conditions": list(base["conditions"]),
    }
    effective = project(base, [_shield()])

    assert effective["derived"]["armor_class"] == 17
    assert base == snapshot, "base 必须原封不动"
    assert effective is not base
    assert effective["derived"] is not base["derived"]


def test_effective_value_is_recomputed_not_stored() -> None:
    """同一份 base + 同一组效果 → 同样结果（纯函数，可缓存）。"""

    base = _base()
    assert project(base, [_shield()]) == project(base, [_shield()])
    assert project(base, []) == base, "没有效果时有效值等于基础值"


def test_buff_expiry_restores_the_base_value() -> None:
    """效果过期后有效值**自动**回到基础值 —— 因为基础值从来没被改过。"""

    base = _base()
    ledger = {UID: []}
    upsert_effect(ledger, ActiveEffect(
        id="dex_potion", target_uid=UID,
        changes=(Change("abilities.dex", "add", 2),),
        duration={"type": "round", "remaining": 2},
    ))
    assert project(base, effects_for(ledger, UID))["abilities"]["dex"] == 18

    assert advance_durations(ledger, "round") == []
    assert project(base, effects_for(ledger, UID))["abilities"]["dex"] == 18, \
        "还剩 1 回合，效果仍在"
    assert ledger[UID][0]["duration"]["remaining"] == 1

    expired = advance_durations(ledger, "round")
    assert [item["id"] for item in expired] == ["dex_potion"]
    assert project(base, effects_for(ledger, UID))["abilities"]["dex"] == 16


# ---------------------------------------------------------------------------
# 2. 算子
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("op", "value", "expected"), [
    ("add", 3, 18),
    ("subtract", 3, 12),
    ("multiply", 2, 30),
    ("override", 21, 21),
])
def test_numeric_operators_on_derived(op: str, value, expected: int) -> None:
    effective = project(_base(), [{
        "id": "x", "changes": [{"key": "derived.armor_class", "op": op, "value": value}],
    }])
    assert effective["derived"]["armor_class"] == expected


def test_upgrade_keeps_higher_so_same_type_bonuses_do_not_stack() -> None:
    """D&D 的"同类加值不叠加"：两个 +2 里只取一个，但取更高的那个。"""

    effects = [
        {"id": "bless", "changes": [
            {"key": "derived.saving_throw", "op": "upgrade", "value": 5},
        ]},
        {"id": "resistance", "changes": [
            {"key": "derived.saving_throw", "op": "upgrade", "value": 4},
        ]},
    ]
    assert project(_base(), effects)["derived"]["saving_throw"] == 5


def test_downgrade_keeps_lower() -> None:
    effective = project(_base(), [{
        "id": "curse", "changes": [
            {"key": "derived.saving_throw", "op": "downgrade", "value": 1},
        ],
    }])
    assert effective["derived"]["saving_throw"] == 1


def test_effects_are_applied_in_order() -> None:
    effects = [
        {"id": "a", "changes": [
            {"key": "derived.armor_class", "op": "add", "value": 5},
        ]},
        {"id": "b", "changes": [
            {"key": "derived.armor_class", "op": "override", "value": 12},
        ]},
    ]
    assert project(_base(), effects)["derived"]["armor_class"] == 12
    assert project(_base(), list(reversed(effects)))["derived"]["armor_class"] == 17


def test_missing_base_path_with_numeric_op_is_rejected() -> None:
    """不能默默把"底子不存在"当成 0 —— 那会把规则写错藏起来。"""

    with pytest.raises(EffectError, match="需要两侧都是数值"):
        project(_base(), [{
            "id": "x", "changes": [
                {"key": "derived.initiative", "op": "add", "value": 2},
            ],
        }])


def test_unresolved_formula_is_rejected() -> None:
    with pytest.raises(EffectError, match="必须在 resolve_intent 里"):
        project(_base(), [{
            "id": "x", "changes": [
                {"key": "derived.armor_class", "op": "add", "value": "1d4"},
            ],
        }])


def test_presentation_cannot_be_written_by_effects() -> None:
    with pytest.raises(EffectError, match="不允许写"):
        project(_base(), [{
            "id": "x", "changes": [
                {"key": "presentation.looks", "op": "override", "value": "发光"},
            ],
        }])


# ---------------------------------------------------------------------------
# 3. 账本：同 id 续期，不叠加
# ---------------------------------------------------------------------------
def test_same_id_refreshes_instead_of_stacking() -> None:
    ledger = {UID: []}
    upsert_effect(ledger, ActiveEffect(
        id="bless", target_uid=UID,
        changes=(Change("derived.saving_throw", "add", 2),),
        duration={"type": "round", "remaining": 3},
    ))
    upsert_effect(ledger, ActiveEffect(
        id="bless", target_uid=UID,
        changes=(Change("derived.saving_throw", "add", 2),),
        duration={"type": "round", "remaining": 9},
    ))

    bucket = ledger[UID]
    assert len(bucket) == 1, "同 id 必须是续期，不是叠一层"
    assert bucket[0]["duration"]["remaining"] == 9
    assert project(_base(), bucket)["derived"]["saving_throw"] == 5


def test_different_ids_stack() -> None:
    """想叠加就换 id —— 这样"为什么是 +4"才有据可查。"""

    ledger = {UID: []}
    for effect_id in ("bless", "heroism"):
        upsert_effect(ledger, ActiveEffect(
            id=effect_id, target_uid=UID,
            changes=(Change("derived.saving_throw", "add", 2),),
        ))
    assert project(_base(), ledger[UID])["derived"]["saving_throw"] == 7


def test_remove_effect_reports_whether_it_removed() -> None:
    ledger = {UID: []}
    upsert_effect(ledger, ActiveEffect(
        id="bless", target_uid=UID,
        changes=(Change("derived.saving_throw", "add", 2),),
    ))
    assert remove_effect(ledger, UID, "bless") is True
    assert remove_effect(ledger, UID, "bless") is False
    assert ledger[UID] == []


def test_ledger_is_bounded_per_actor() -> None:
    ledger = {UID: []}
    for index in range(MAX_EFFECTS_PER_ACTOR + 5):
        upsert_effect(ledger, ActiveEffect(
            id=f"e{index}", target_uid=UID,
            changes=(Change("derived.saving_throw", "add", 1),),
        ))
    assert len(ledger[UID]) == MAX_EFFECTS_PER_ACTOR
    assert ledger[UID][-1]["id"] == f"e{MAX_EFFECTS_PER_ACTOR + 4}"


def test_effects_are_per_actor() -> None:
    ledger: dict = {}
    upsert_effect(ledger, ActiveEffect(
        id="bless", target_uid=UID,
        changes=(Change("derived.saving_throw", "add", 2),),
    ))
    assert len(effects_for(ledger, UID)) == 1
    assert effects_for(ledger, "player:other") == []


# ---------------------------------------------------------------------------
# 4. 校验与时长
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("effect", "message"), [
    (ActiveEffect(id="", target_uid=UID, changes=(Change("derived.x", "add", 1),)), "缺少 id"),
    (ActiveEffect(id="x", target_uid="", changes=(Change("derived.x", "add", 1),)), "缺少 target_uid"),
    (ActiveEffect(id="x", target_uid=UID, changes=()), "没有任何修正量"),
    (ActiveEffect(id="x", target_uid=UID, changes=(Change("", "add", 1),)), "缺少 key"),
    (ActiveEffect(id="x", target_uid=UID, changes=(Change("derived.x", "toggle", 1),)), "未知"),
    (ActiveEffect(id="x", target_uid=UID, changes=(Change("derived.x", "add", 1),),
                  duration={"type": "eon", "remaining": 1}), "时长类型"),
    (ActiveEffect(id="x", target_uid=UID, changes=(Change("derived.x", "add", 1),),
                  duration={"type": "round", "remaining": -1}), "不能为负"),
])
def test_active_effect_validation(effect: ActiveEffect, message: str) -> None:
    with pytest.raises(EffectError, match=message):
        effect.validate()


def test_load_effects_validates_shape() -> None:
    assert load_effects(None) == {}
    assert load_effects({}) == {}
    with pytest.raises(EffectError, match="必须是"):
        load_effects(["nope"])
    with pytest.raises(EffectError, match="必须是数组"):
        load_effects({UID: "nope"})


def test_rest_and_scene_clear_matching_durations_outright() -> None:
    """休息不是按次数计的 —— 对应类型的效果一次性清空，不递减。"""

    ledger = {UID: []}
    upsert_effect(ledger, ActiveEffect(
        id="bless", target_uid=UID,
        changes=(Change("derived.saving_throw", "add", 2),),
        duration={"type": "rest", "remaining": 5},
    ))
    upsert_effect(ledger, ActiveEffect(
        id="shield", target_uid=UID,
        changes=(Change("derived.armor_class", "add", 2),),
        duration={"type": "round", "remaining": 5},
    ))

    expired = advance_durations(ledger, "rest")
    assert [item["id"] for item in expired] == ["bless"]
    assert [item["id"] for item in ledger[UID]] == ["shield"], "不匹配的类型动都不动"
    assert ledger[UID][0]["duration"]["remaining"] == 5


def test_advance_durations_only_touches_matching_kind() -> None:
    ledger = {UID: []}
    for kind in ("round", "turn"):
        upsert_effect(ledger, ActiveEffect(
            id=f"e_{kind}", target_uid=UID,
            changes=(Change("derived.saving_throw", "add", 1),),
            duration={"type": kind, "remaining": 3},
        ))
    advance_durations(ledger, "round")
    by_id = {item["id"]: item for item in ledger[UID]}
    assert by_id["e_round"]["duration"]["remaining"] == 2
    assert by_id["e_turn"]["duration"]["remaining"] == 3


def test_permanent_effects_have_no_duration() -> None:
    ledger = {UID: []}
    upsert_effect(ledger, ActiveEffect(
        id="magic_weapon", target_uid=UID,
        changes=(Change("derived.attack_bonus", "upgrade", 1),),
        duration=None,
    ))
    for kind in ("round", "turn", "rest", "scene"):
        assert advance_durations(ledger, kind) == []
    assert len(ledger[UID]) == 1


def test_unknown_duration_kind_is_rejected() -> None:
    with pytest.raises(EffectError, match="未知的计时单位"):
        advance_durations({UID: []}, "fortnight")
