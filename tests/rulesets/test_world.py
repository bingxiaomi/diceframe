"""世界状态层（``src/rulesets/world.py``）的单元测试。

纯数据 + 纯函数，不需要服务 / LLM / 前端。重点验四件事：

1. **权威与呈现单向** —— ``presentation.*`` 写不进去（结构性保证，不是约定）；
2. **算子语义不猜** —— 布尔字段只支持 override；公式字符串直接报错；
3. **版本只在真变化时自增** —— 这是防刷骰失效判定的支点；
4. **失败即整批失败** —— 不自动创建对象、不静默跳过。
"""

from __future__ import annotations

import pytest

from src.rulesets.adjudication import EffectDescriptor
from src.rulesets.world import (
    WORLD_TARGET_NOT_FOUND,
    WorldError,
    WorldObject,
    apply_effect,
    apply_effects,
    dump_world,
    load_world,
    read_field,
)


def _door(**state) -> dict:
    return {
        "kind": "door",
        "state": {"locked": True, "open": False, "hp": 20},
        "tags": ["wooden", "interactable"],
        "presentation": {"description": "一扇沉重而古老的门"},
        "version": 7,
    }


def _world() -> dict:
    return {"door_warehouse_01": _door()}


def _effect(**overrides) -> EffectDescriptor:
    payload = {
        "kind": "world",
        "target": "door_warehouse_01",
        "field": "state.locked",
        "op": "override",
        "value": False,
    }
    payload.update(overrides)
    return EffectDescriptor(**payload)


# ---------------------------------------------------------------------------
# 1. 对象形状
# ---------------------------------------------------------------------------
def test_object_roundtrip_is_stable() -> None:
    obj = WorldObject.from_dict("door_01", _door())
    obj.validate()
    dumped = obj.to_dict()
    assert dumped["tags"] == ["interactable", "wooden"], "tags 要排序以保证落盘稳定"
    assert dumped["version"] == 7
    assert WorldObject.from_dict("door_01", dumped) == obj


def test_object_rejects_missing_kind_and_negative_version() -> None:
    with pytest.raises(WorldError, match="缺少 kind"):
        WorldObject.from_dict("x", {"state": {}})
    with pytest.raises(WorldError, match="version 不能为负"):
        WorldObject.from_dict("x", {"kind": "door", "version": -1})


def test_object_rejects_presentation_shadowing_state() -> None:
    """权威值与呈现不得同名 —— 否则叙事会污染事实。"""

    with pytest.raises(WorldError, match="权威值与呈现必须分开"):
        WorldObject.from_dict("x", {
            "kind": "door", "state": {"locked": True},
            "presentation": {"locked": "看起来锁死了"},
        })


def test_world_load_and_dump() -> None:
    world = load_world(_world())
    assert set(world) == {"door_warehouse_01"}
    assert world["door_warehouse_01"].state["locked"] is True
    assert load_world(None) == {}
    assert load_world({}) == {}
    with pytest.raises(WorldError, match="world 必须是"):
        load_world(["not", "a", "mapping"])
    assert dump_world(world)["door_warehouse_01"]["kind"] == "door"


def test_read_field_walks_dotted_paths() -> None:
    state = {"stats": {"hp": 12}, "locked": False}
    assert read_field(state, "stats.hp") == 12
    assert read_field(state, "locked") is False
    assert read_field(state, "missing") is None
    assert read_field(state, "stats.missing.deeper") is None
    assert read_field(state, "") is None


# ---------------------------------------------------------------------------
# 2. 权威 ← 呈现是单向的
# ---------------------------------------------------------------------------
def test_presentation_is_not_writable_by_rules() -> None:
    """叙事层一句"门缓缓打开"不能变成世界事实。"""

    world = _world()
    with pytest.raises(WorldError, match="不允许通过效果写 presentation"):
        apply_effect(world, _effect(field="presentation.description", op="override", value="开了"))


@pytest.mark.parametrize("path", ["locked", "kind", "version", "hp"])
def test_effect_path_must_be_state_or_tags(path: str) -> None:
    with pytest.raises(WorldError, match=r"必须以 state\. 或 tags 开头"):
        apply_effect(_world(), _effect(field=path))


def test_tags_path_rejects_subpaths() -> None:
    with pytest.raises(WorldError, match="tags 不支持子路径"):
        apply_effect(_world(), _effect(field="tags.wooden"))


# ---------------------------------------------------------------------------
# 3. 目标不存在时失败，不自动创建
# ---------------------------------------------------------------------------
def test_unknown_target_fails_closed() -> None:
    world = _world()
    with pytest.raises(WorldError, match=WORLD_TARGET_NOT_FOUND):
        apply_effect(world, _effect(target="door_ghost"))
    assert set(world) == {"door_warehouse_01"}, "不能凭空创建对象"


# ---------------------------------------------------------------------------
# 4. 算子语义
# ---------------------------------------------------------------------------
def test_override_sets_value_and_reports_change() -> None:
    world = _world()
    assert apply_effect(world, _effect(value=False)) is True
    assert world["door_warehouse_01"]["state"]["locked"] is False


def test_override_to_same_value_reports_no_change() -> None:
    """写了但值没变 —— 不该让绑定同一版本的尝试记录失效。"""

    world = _world()
    assert apply_effect(world, _effect(value=True)) is False


@pytest.mark.parametrize(("op", "value", "expected"), [
    ("add", 5, 25),
    ("subtract", 5, 15),
    ("multiply", 2, 40),
])
def test_numeric_operators(op: str, value: float, expected: float) -> None:
    world = _world()
    assert apply_effect(world, _effect(field="state.hp", op=op, value=value)) is True
    assert world["door_warehouse_01"]["state"]["hp"] == expected


def test_upgrade_keeps_higher_and_downgrade_keeps_lower() -> None:
    """Foundry 的 upgrade/downgrade —— D&D 的"取最高加值"叠法靠它。"""

    world = _world()
    assert apply_effect(world, _effect(field="state.hp", op="upgrade", value=30)) is True
    assert world["door_warehouse_01"]["state"]["hp"] == 30
    assert apply_effect(world, _effect(field="state.hp", op="upgrade", value=10)) is False
    assert world["door_warehouse_01"]["state"]["hp"] == 30

    assert apply_effect(world, _effect(field="state.hp", op="downgrade", value=5)) is True
    assert world["door_warehouse_01"]["state"]["hp"] == 5
    assert apply_effect(world, _effect(field="state.hp", op="downgrade", value=99)) is False


def test_boolean_fields_only_accept_override() -> None:
    with pytest.raises(WorldError, match="布尔字段只支持 override"):
        apply_effect(_world(), _effect(op="add", value=1))
    with pytest.raises(WorldError, match="布尔字段只支持 override"):
        apply_effect(_world(), _effect(field="state.open", op="subtract", value=1))


def test_unresolved_formula_is_rejected_not_guessed() -> None:
    """公式必须在 resolve_intent 里用服务端 RNG 解析 —— 这里看到公式要报错。"""

    with pytest.raises(WorldError, match="必须在 resolve_intent 里"):
        apply_effect(_world(), _effect(field="state.hp", op="subtract", value="1d6"))


def test_numeric_operator_on_non_numeric_field_is_rejected() -> None:
    world = _world()
    world["door_warehouse_01"]["state"]["material"] = "oak"
    with pytest.raises(WorldError, match="需要两侧都是数值"):
        apply_effect(world, _effect(field="state.material", op="add", value=1))


def test_string_fields_accept_override() -> None:
    world = _world()
    world["door_warehouse_01"]["state"]["material"] = "oak"
    assert apply_effect(
        world, _effect(field="state.material", op="override", value="steel"),
    ) is True
    assert world["door_warehouse_01"]["state"]["material"] == "steel"


def test_nested_state_paths_are_created_on_demand() -> None:
    world = _world()
    assert apply_effect(
        world, _effect(field="state.damage.cracks", op="override", value=2),
    ) is True
    assert world["door_warehouse_01"]["state"]["damage"]["cracks"] == 2


# ---------------------------------------------------------------------------
# 5. tags = grant / deny
# ---------------------------------------------------------------------------
def test_tags_add_is_grant_and_subtract_is_revoke() -> None:
    world = _world()
    assert apply_effect(world, _effect(field="tags", op="add", value="broken")) is True
    assert world["door_warehouse_01"]["tags"] == ["broken", "interactable", "wooden"]

    assert apply_effect(world, _effect(field="tags", op="add", value=["wooden"])) is False
    assert apply_effect(world, _effect(field="tags", op="subtract", value="interactable")) is True
    assert "interactable" not in world["door_warehouse_01"]["tags"]


def test_tags_reject_non_set_operators() -> None:
    with pytest.raises(WorldError, match="只支持 add（授予）/ subtract（剥夺）"):
        apply_effect(_world(), _effect(field="tags", op="override", value="x"))


# ---------------------------------------------------------------------------
# 6. 批量应用：版本、顺序、失败即整批失败
# ---------------------------------------------------------------------------
def test_batch_bumps_version_once_per_object_and_only_on_change() -> None:
    world = _world()
    touched = apply_effects(world, [
        _effect(field="state.locked", value=False),   # 变了
        _effect(field="state.open", value=True),      # 变了
        _effect(field="state.open", value=True),      # 没变
    ])
    assert touched == ["door_warehouse_01"]
    assert world["door_warehouse_01"]["version"] == 9, "两次真变化，从 7 到 9"


def test_batch_order_matters() -> None:
    world = _world()
    apply_effects(world, [
        _effect(field="state.hp", op="override", value=10),
        _effect(field="state.hp", op="subtract", value=3),
    ])
    assert world["door_warehouse_01"]["state"]["hp"] == 7


def test_batch_fails_whole_batch_on_bad_effect() -> None:
    """中途失败就抛 —— 回滚交给调用方（宿主的事务快照），这里不自己回滚。"""

    world = _world()
    with pytest.raises(WorldError, match=WORLD_TARGET_NOT_FOUND):
        apply_effects(world, [
            _effect(field="state.locked", value=False),
            _effect(target="door_ghost"),
        ])


def test_batch_rejects_non_descriptor_entries() -> None:
    with pytest.raises(WorldError, match="非效果描述符"):
        apply_effects(_world(), ["state.locked"])


def test_empty_batch_is_a_noop() -> None:
    world = _world()
    assert apply_effects(world, []) == []
    assert world["door_warehouse_01"]["version"] == 7


# ---------------------------------------------------------------------------
# 7. 防刷骰的失效判定（设计意图的回归测试）
# ---------------------------------------------------------------------------
def test_gm_relocking_invalidates_earlier_attempt_via_version() -> None:
    """GM 重新锁门 → version 变了 → 旧尝试自动失效，不需要手工 reset。"""

    world = _world()
    version_before = world["door_warehouse_01"]["version"]

    # 玩家撬锁失败，并且这次失败把锁弄卡住了（改了对象）。
    apply_effects(world, [_effect(field="state.locked", value=False)])
    attempt_world_version = world["door_warehouse_01"]["version"]
    assert attempt_world_version == version_before + 1

    # GM 重新锁门 —— 这就是"变化"，不需要任何 clear_attempts()。
    apply_effects(world, [_effect(field="state.locked", value=True)])
    assert world["door_warehouse_01"]["version"] != attempt_world_version
