"""``custom:declarative`` 的世界效果端到端：声明 → 事件 → 真正改变世界状态。

这条链路补的是"裁定落不了地"：``resolve_intent`` 早就在发事件，
但 ``apply_event_batch`` 以前会把不认识的事件静默丢掉。

同时验证两件事：
- **首次播种不覆盖** —— 已解锁的门不该在下一批被规则声明重新锁上；
- **声明期 fail-fast** —— 引用未声明的对象、写 ``presentation.*`` 都要在加载时炸，
  而不是跑团中途变成"莫名其妙没生效"。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from src.engine.game_instance import GameRegistry
from src.rules.loader import RuleBundleLoader
from src.rules.rule_system import RuleSystem
from src.rulesets.contracts import RulesetCapabilities
from src.rulesets.custom.manifest import parse_custom_mechanics
from src.rulesets.custom.runtime import CustomDeclarativeRuntime

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "templates" / "rules"
RULE_ID = "custom_freeform"
UID = "world_player"

DOOR = "sealed_door"


class _FixedRng:
    """每次 randint 返回固定值 —— 让检定档位可预测。

    ``1d100`` 掷出 1、目标 50 → 比例 0.02 → 落在 ``extreme`` 档（max_ratio 0.2）。
    """

    def __init__(self, value: int = 1) -> None:
        self.value = value

    def randint(self, low: int, high: int) -> int:  # noqa: ARG002 - 契约要求签名
        return self.value


class _ProfessionalRuntime(CustomDeclarativeRuntime):
    capabilities = RulesetCapabilities(
        experience_profile="custom",
        character_builder="professional",
        character_lifecycle="rules_aware",
        authoritative_intents=True,
        narrative_turns=True,
    )


@pytest.fixture(scope="module")
def runtime() -> _ProfessionalRuntime:
    return _ProfessionalRuntime()


@pytest.fixture(scope="module")
def rule() -> RuleSystem:
    return RuleSystem(RuleBundleLoader().load_rule(RULES_DIR, RULE_ID, ""))


def _game(tmp_path: Path, key: str = "w1"):
    registry = GameRegistry(tmp_path / "saves")
    instance = registry.get_or_create(("web", "world", key))
    instance.world_id = "test_fantasy"
    instance.rule_id = RULE_ID
    return registry, instance


def _ready(runtime, rule, tmp_path: Path, key: str = "w1"):
    """走完建卡 + 入局，拿到一个可以提交意图的对局。"""

    _registry, instance = _game(tmp_path, key)
    card = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, {"attributes": {
            "str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10,
        }}), "zh-CN",
    )
    assert instance.bind_ruleset_runtime(card["rule_binding"]) is True
    instance.players[UID] = {
        "character_name": card["character_name"],
        "character_sheet": {
            "attributes": card["attributes"],
            "hp": card["hp"], "max_hp": card["max_hp"],
            "ruleset_character": deepcopy(card["ruleset_character"]),
        },
    }
    runtime.on_player_join(instance, UID)
    return instance


def _submit(runtime, instance, rng) -> dict:
    intent = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": "will_check"}, UID, False,
    )
    resolved = runtime.resolve_intent(instance, intent, rng)
    assert resolved["ok"] is True, resolved
    return runtime.apply_event_batch(instance, resolved["event_batch"])


# ---------------------------------------------------------------------------
# 1. 声明 → 世界状态
# ---------------------------------------------------------------------------
def test_world_is_seeded_on_first_use(runtime, rule, tmp_path) -> None:
    """初始对象表只在首次用到世界状态时播种。"""

    instance = _ready(runtime, rule, tmp_path, "seed")
    assert "world" not in instance.ruleset_state

    _submit(runtime, instance, _FixedRng(1))

    world = instance.ruleset_state["world"]
    assert set(world) == {DOOR}
    assert world[DOOR]["kind"] == "door"
    assert world[DOOR]["tags"] == ["interactable", "wooden"]
    assert world[DOOR]["version"] > 0


def test_presentation_stays_separate_from_state(runtime, rule, tmp_path) -> None:
    """呈现与权威分离：描述在 presentation，规则字段在 state。"""

    instance = _ready(runtime, rule, tmp_path, "pres")
    _submit(runtime, instance, _FixedRng(1))

    door = instance.ruleset_state["world"][DOOR]
    assert "description" in door["presentation"]
    assert "description" not in door["state"]
    assert set(door["state"]) >= {"locked", "open", "hp"}


def test_extreme_degree_unlocks_the_door(runtime, rule, tmp_path) -> None:
    """``world_effects`` 里 extreme → ``state.locked override false`` 真的生效。"""

    instance = _ready(runtime, rule, tmp_path, "unlock")
    applied = _submit(runtime, instance, _FixedRng(1))

    door = instance.ruleset_state["world"][DOOR]
    assert door["state"]["locked"] is False, "极难成功应当解锁"
    assert door["state"]["open"] is False, "只解锁，不该顺手把门推开"

    world_changes = [e for e in applied["events"] if e["type"] == "world.changed"]
    assert world_changes, "世界变更必须出现在 reducer 输出里"
    assert world_changes[0]["objects"] == [DOOR]


def test_failure_degree_damages_the_door(runtime, rule, tmp_path) -> None:
    """``subtract delta: 1d6`` 在服务端用 RNG 解析后落到 hp。"""

    instance = _ready(runtime, rule, tmp_path, "damage")
    # 掷 100：1d100 与目标 50 的比例 2.0 > 1.0 → failure（fallback 档）
    _submit(runtime, instance, _FixedRng(100))

    door = instance.ruleset_state["world"][DOOR]
    assert door["state"]["hp"] < 20, "失败应当对门造成损伤"
    assert door["state"]["locked"] is True, "失败不该解锁"


def test_seeding_does_not_resurrect_old_state(runtime, rule, tmp_path) -> None:
    """播种**不覆盖**已有对象 —— 否则已解锁的门会被声明重新锁上。"""

    instance = _ready(runtime, rule, tmp_path, "reseed")
    _submit(runtime, instance, _FixedRng(1))
    assert instance.ruleset_state["world"][DOOR]["state"]["locked"] is False

    # 再来一次：世界已经存在，声明不该把它重置回 locked: true
    _submit(runtime, instance, _FixedRng(100))
    assert instance.ruleset_state["world"][DOOR]["state"]["locked"] is False


# ---------------------------------------------------------------------------
# 2. 声明期 fail-fast
# ---------------------------------------------------------------------------
def _mechanics(**overrides) -> dict:
    base = {
        "checks": [{
            "id": "c1",
            "degrees": [
                {"id": "good", "max_ratio": 1.0},
                {"id": "bad", "fallback": True},
            ],
        }],
        "world": [{"id": "box", "kind": "chest", "state": {"open": False}}],
        "world_effects": [{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.open", "op": "override", "value": True,
        }],
    }
    base.update(overrides)
    return {"custom_mechanics": base}


def test_declaration_parses_world_and_world_effects() -> None:
    mechanics = parse_custom_mechanics(_mechanics())
    assert [o.id for o in mechanics.world] == ["box"]
    assert mechanics.world_seed()["box"]["state"] == {"open": False}
    assert [e.op for e in mechanics.world_effects] == ["override"]


def test_undeclared_world_target_is_rejected() -> None:
    """拼错对象名必须在加载时炸，而不是跑团中途。"""

    with pytest.raises(ValueError, match="未声明的世界对象"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "ghost",
            "field": "state.open", "op": "override", "value": True,
        }]))


def test_presentation_cannot_be_written_by_declaration() -> None:
    with pytest.raises(ValueError, match="presentation 写不进去"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "presentation.description", "op": "override", "value": "开了",
        }]))


def test_presentation_may_not_shadow_state_keys() -> None:
    with pytest.raises(ValueError, match="权威值与呈现必须分开"):
        parse_custom_mechanics(_mechanics(world=[{
            "id": "box", "kind": "chest",
            "state": {"open": False},
            "presentation": {"open": "看起来是关的"},
        }]))


def test_override_requires_literal_value_and_no_delta() -> None:
    with pytest.raises(ValueError, match="必须给出非 null 的 value"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.open", "op": "override",
        }]))
    with pytest.raises(ValueError, match="override 不能同时给 delta"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.hp", "op": "override", "value": 1, "delta": "1d6",
        }]))


def test_numeric_op_needs_exactly_one_of_value_or_delta() -> None:
    with pytest.raises(ValueError, match="必须且只能给出 value 或 delta 之一"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.hp", "op": "add",
        }]))
    with pytest.raises(ValueError, match="必须且只能给出 value 或 delta 之一"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.hp", "op": "add", "value": 1, "delta": "1d6",
        }]))


def test_tags_only_accept_set_operators() -> None:
    with pytest.raises(ValueError, match="只支持 add（授予）/ subtract（剥夺）"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "tags", "op": "override", "value": "x",
        }]))
    with pytest.raises(ValueError, match="集合不能配骰式"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "tags", "op": "add", "delta": "1d6",
        }]))


def test_unknown_operator_is_rejected() -> None:
    with pytest.raises(ValueError, match="op 必须是"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.open", "op": "toggle", "value": True,
        }]))


def test_attempt_metadata_requires_both_family_and_policy() -> None:
    """不设默许值：默许成 FREE 等于"失败不留痕"，会让检定彻底失去意义。"""

    with pytest.raises(ValueError, match="必须同时给出"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.open", "op": "override", "value": True,
            "intent_family": "open",
        }]))
    with pytest.raises(ValueError, match="必须同时给出"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.open", "op": "override", "value": True,
            "retry_policy": "FREE",
        }]))


def test_attempt_metadata_rejects_unknown_policy() -> None:
    with pytest.raises(ValueError, match="retry_policy 必须是"):
        parse_custom_mechanics(_mechanics(world_effects=[{
            "check": "c1", "degree": "good", "target": "box",
            "field": "state.open", "op": "override", "value": True,
            "intent_family": "open", "retry_policy": "MAYBE",
        }]))


def test_world_effect_without_attempt_metadata_creates_no_attempt() -> None:
    """只声明世界变更、不声明尝试元数据 → 与重试无关。"""

    mechanics = parse_custom_mechanics(_mechanics(world_effects=[{
        "check": "c1", "degree": "good", "target": "box",
        "field": "state.open", "op": "override", "value": True,
    }]))
    assert mechanics.world_effects[0].intent_family == ""
    assert mechanics.world_effects[0].retry_policy == ""


# ---------------------------------------------------------------------------
# 3. 接线：提交时记账，且记的是**落盘之后**的版本
# ---------------------------------------------------------------------------
def test_failure_records_attempt_with_post_commit_version(runtime, rule, tmp_path) -> None:
    """账本里的世界版本必须等于落盘后的版本 —— 这是防无限重试的关键。"""

    instance = _ready(runtime, rule, tmp_path, "rec")
    _submit(runtime, instance, _FixedRng(100))  # failure → hp 扣血、版本自增

    attempts = instance.ruleset_state["attempts"]
    assert attempts, "失败必须留下尝试记录"
    entry = attempts[-1]
    assert entry["outcome"] == "FAILURE"
    assert entry["intent_family"] == "open"
    assert entry["approach_signature"] == "force"
    assert entry["retry_policy"] == "REQUIRES_CHANGED_CIRCUMSTANCE"
    assert entry["world_versions"] == {
        DOOR: instance.ruleset_state["world"][DOOR]["version"]
    }, "记的必须是落盘后的版本"


def test_same_approach_is_blocked_after_failure(runtime, rule, tmp_path) -> None:
    """试过了、锁也卡住了 → 同样办法再来一次会被拦住，失败有意义。"""

    instance = _ready(runtime, rule, tmp_path, "block")
    _submit(runtime, instance, _FixedRng(100))

    status = runtime.retry_status(
        instance, actor_id=UID, intent_family="open", target_id=DOOR,
        approach_signature="force",
    )
    assert status["allowed"] is False
    assert status["failure_matters"] is True
    assert status["policy"] == "REQUIRES_CHANGED_CIRCUMSTANCE"


def test_different_approach_is_not_blocked(runtime, rule, tmp_path) -> None:
    """撬锁失败不该挡住"拿斧头砍门" —— 换手段就是另一个 key。"""

    instance = _ready(runtime, rule, tmp_path, "other")
    _submit(runtime, instance, _FixedRng(100))

    status = runtime.retry_status(
        instance, actor_id=UID, intent_family="open", target_id=DOOR,
        approach_signature="finesse",
    )
    assert status["allowed"] is True
    assert status["attempt"] is None, "另一个 key 查不到记录"


def test_gm_changing_the_world_allows_retry(runtime, rule, tmp_path) -> None:
    """GM 重新锁门 → 版本变 → 旧记录自动失效，不需要任何手工清理。"""

    instance = _ready(runtime, rule, tmp_path, "relock")
    _submit(runtime, instance, _FixedRng(100))
    assert runtime.retry_status(
        instance, actor_id=UID, intent_family="open", target_id=DOOR,
        approach_signature="force",
    )["allowed"] is False

    # GM 重新锁门（等价于任何让世界变了的操作）
    instance.ruleset_state["world"][DOOR]["version"] += 1

    assert runtime.retry_status(
        instance, actor_id=UID, intent_family="open", target_id=DOOR,
        approach_signature="force",
    )["allowed"] is True


def test_success_is_recorded_but_does_not_block(runtime, rule, tmp_path) -> None:
    """成功后不存在"还试不试得动"的问题。"""

    instance = _ready(runtime, rule, tmp_path, "ok")
    _submit(runtime, instance, _FixedRng(1))  # extreme → 解锁

    assert instance.ruleset_state["attempts"][-1]["outcome"] == "EXTREME" \
        or instance.ruleset_state["attempts"][-1]["outcome"] == "CRITICAL_SUCCESS"
    assert runtime.retry_status(
        instance, actor_id=UID, intent_family="open", target_id=DOOR,
        approach_signature="force",
    )["allowed"] is True


def test_retry_status_on_empty_history_is_conservative(runtime, rule, tmp_path) -> None:
    instance = _ready(runtime, rule, tmp_path, "fresh")
    status = runtime.retry_status(
        instance, actor_id=UID, intent_family="open", target_id=DOOR,
        approach_signature="force",
    )
    assert status["allowed"] is True
    assert status["attempt"] is None
    assert status["failure_matters"] is True, "没有证据时不能替规则文本下结论"
