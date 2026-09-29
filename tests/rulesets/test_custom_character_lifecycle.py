"""``custom:declarative`` 的专业建卡：绑定与规则快照必须**自动**发生。

建卡本身不是目标，让两道手工门槛变成自动的才是：

    门槛②  存档被绑定（``bind_ruleset_runtime``）
    门槛③  规则声明进了 ``ruleset_state["mechanics"]``

做法是 ``character_builder="professional"`` + ``character_lifecycle="rules_aware"``。
这两项**现在是发布默认值** —— 它们曾经关闭，因为 ``describe_experience`` 会返回
``profile="custom"`` 而前端注册表当时没有对应组件，打开会让入局 / 建房页报
``Unsupported ruleset experience``。组件现已落地
（``frontend-v2/src/features/rulesets/custom/``），所以默认翻转。
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import pytest

from src.engine.game_instance import GameRegistry
from src.rules.loader import RuleBundleLoader
from src.rules.rule_system import RuleSystem
from src.rulesets.contracts import RulesetCapabilities
from src.rulesets.custom import runtime as custom_runtime
from src.rulesets.custom.runtime import CustomDeclarativeRuntime

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "templates" / "rules"
RULE_ID = "custom_freeform"
UID = "stage0_player"

BALANCED = {
    "str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10,
}


class _ProfessionalRuntime(CustomDeclarativeRuntime):
    """只覆盖能力位，其余全部走真实现。"""

    capabilities = RulesetCapabilities(
        experience_profile="custom",
        character_builder="professional",
        character_lifecycle="rules_aware",
        authoritative_intents=True,
        narrative_turns=True,
        deterministic_combat=False,
        versioned_state=False,
    )


@pytest.fixture(scope="module")
def runtime() -> _ProfessionalRuntime:
    return _ProfessionalRuntime()


@pytest.fixture(scope="module")
def rule() -> RuleSystem:
    return RuleSystem(RuleBundleLoader().load_rule(RULES_DIR, RULE_ID, ""))


def _draft(**overrides) -> dict:
    draft = {
        "name": "测试者",
        "race": "人类",
        "class": "冒险者",
        "attributes": dict(BALANCED),
        "skills": ["侦查"],
    }
    draft.update(overrides)
    return draft


def _game(tmp_path: Path, key: str = "p1"):
    registry = GameRegistry(tmp_path / "saves")
    instance = registry.get_or_create(("web", "stage0", key))
    instance.world_id = "test_fantasy"
    instance.rule_id = RULE_ID
    return registry, instance


def _join(instance, card: dict, uid: str = UID, runtime=None) -> None:
    """复现宿主侧 ``characters.create_player`` 的关键两步：绑定 + 通知加入。"""

    assert instance.bind_ruleset_runtime(card["rule_binding"]) is True
    instance.players[uid] = {
        "character_name": card["character_name"],
        "character_sheet": {
            "attributes": card["attributes"],
            "hp": card["hp"],
            "max_hp": card["max_hp"],
            "rule_binding": deepcopy(card["rule_binding"]),
            "ruleset_character": deepcopy(card["ruleset_character"]),
        },
    }
    (runtime or _ProfessionalRuntime()).on_player_join(instance, uid)


# ---------------------------------------------------------------------------
# 发布默认值
# ---------------------------------------------------------------------------
_SWITCHES = (
    "DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS",
    "DICEFRAME_CUSTOM_PROFESSIONAL_BUILDER",
)


def test_default_capabilities_are_on() -> None:
    """发布默认值是**开**。

    它守的是**发布默认值**，不是当前进程的设置：默认值一旦被改回去，前端组件
    与建卡分支的假设会同时失效（入局页报 ``Unsupported ruleset experience``，
    而存档也不会再被绑定）。

    有人显式设了环境变量时，这里测的就不是默认值了，所以跳过并说明 —— 否则
    "我临时退回 Stage A 跑一下测试"会得到一条看不懂的失败。
    """

    overridden = [name for name in _SWITCHES if name in os.environ]
    if overridden:
        pytest.skip(f"环境变量已覆盖默认值：{', '.join(overridden)}")

    capabilities = CustomDeclarativeRuntime.capabilities
    assert capabilities.character_builder == "professional"
    assert capabilities.character_lifecycle == "rules_aware"
    assert capabilities.authoritative_intents is True


def test_env_flag_never_treats_an_explicit_zero_as_on() -> None:
    """``_env_flag``：**未设置或只有空白**才用默认值；显式写了就按真假集合解析。

    "非空即真"会让 ``=0`` 变成开 —— 那是最反直觉的一类开关 bug，而默认翻转之后
    它正好是最常用的那一个值。
    """

    name = "DICEFRAME_TEST_FLAG_PROBE"
    os.environ.pop(name, None)
    try:
        assert custom_runtime._env_flag(name) is False
        assert custom_runtime._env_flag(name, default=True) is True

        for raw, expected in (
            ("0", False), ("false", False), ("off", False), ("no", False),
            ("1", True), ("true", True), ("YES", True), ("ON", True),
        ):
            os.environ[name] = raw
            assert custom_runtime._env_flag(name, default=True) is expected, raw

        os.environ[name] = "   "
        assert custom_runtime._env_flag(name, default=True) is True, "空白 = 未设置"
    finally:
        os.environ.pop(name, None)


# ---------------------------------------------------------------------------
# 6 个建卡方法
# ---------------------------------------------------------------------------
def test_describe_experience_reports_professional(runtime, rule) -> None:
    experience = runtime.describe_experience(rule, "zh-CN")
    assert experience["profile"] == "custom"
    assert experience["builder_mode"] == "professional"
    assert experience["content_version"]
    assert [item["id"] for item in experience["resources"]] == ["resolve"]


def test_builder_choices_expose_materials(runtime, rule) -> None:
    choices = runtime.builder_choices(rule, {})
    assert choices["attribute_points"] == 60
    assert [row["key"] for row in choices["attributes"]] == list(BALANCED)
    assert all(row["min"] == 3 and row["max"] == 18 for row in choices["attributes"])
    assert [item["id"] for item in choices["resources"]] == ["resolve"]
    assert choices["attr_hint"]


def test_quick_presets_are_directly_finalizable(runtime, rule) -> None:
    """预设里的 draft 必须能原样喂给 finalize —— 前端一键建卡靠这条契约。"""

    presets = runtime.builder_choices(rule, {})["quick_presets"]
    assert presets, "至少要给一个可一键建卡的预设"
    for preset in presets:
        assert isinstance(preset["draft"], dict)
        assert preset["recommendation_reason"]
        card = runtime.finalize_character(rule, preset["draft"])
        assert card["ruleset_character"]["abilities"], preset["id"]
        total = sum(card["ruleset_character"]["abilities"].values())
        assert total <= 60, f"{preset['id']} 超出点购预算"


def test_validate_character_accepts_and_rejects(runtime, rule) -> None:
    assert runtime.validate_character(rule, _draft()) == []
    too_high = runtime.validate_character(rule, _draft(attributes={**BALANCED, "str": 999}))
    assert any("超出" in message for message in too_high)
    missing = runtime.validate_character(rule, {"attributes": {"str": 10}})
    assert any("缺少属性" in message for message in missing)
    overspent = runtime.validate_character(
        rule, _draft(attributes={**BALANCED, "str": 18, "dex": 18}),
    )
    assert any("超过上限" in message for message in overspent)


def test_derive_raises_on_invalid_draft(runtime, rule) -> None:
    """``ruleset_builder`` 与 ``create_player`` 都靠这个 ValueError 报错。"""

    with pytest.raises(ValueError):
        runtime.derive_character(rule, _draft(attributes={**BALANCED, "str": 999}))


def test_derive_carries_binding_and_declaration(runtime, rule) -> None:
    canonical = runtime.derive_character(rule, _draft())
    binding = canonical["rule_binding"]
    assert binding["rule_id"] == RULE_ID
    assert binding["runtime_id"] == "custom:declarative"
    assert binding["runtime_version"] == 1
    assert binding["content_version"]
    assert binding["state_schema_version"] == 1
    # 规则声明必须随卡走 —— 这是游戏期唯一能读到规则的来源
    assert canonical["mechanics"]["resources"][0]["id"] == "resolve"
    assert canonical["mechanics"]["checks"][0]["id"] == "will_check"
    assert canonical["derived"]["hp"] > 0
    assert canonical["abilities"] == BALANCED
    assert canonical["locale"] == ""


def test_finalize_matches_engine_contract(runtime, rule) -> None:
    """``characters.py`` 读 ``character["ruleset_character"]["rule_binding"]``。"""

    card = runtime.finalize_character(rule, _draft())
    for key in ("character_name", "attributes", "hp", "max_hp", "armor_class",
                "rule_binding", "ruleset_character"):
        assert key in card, key
    assert card["ruleset_character"]["rule_binding"] == card["rule_binding"]
    assert card["ruleset_character"]["rule_binding"]["runtime_id"] == "custom:declarative"


def test_normalize_discards_client_derived_values(runtime, rule) -> None:
    """客户端提交的派生数值必须被丢弃重算（不是信任，是重算）。"""

    card = runtime.finalize_character(rule, _draft())
    tampered = {**card, "hp": 999, "max_hp": 999, "armor_class": 99}
    normalized = runtime.normalize_character_submission(rule, tampered, "zh-CN")
    assert normalized["hp"] == card["hp"]
    assert normalized["max_hp"] == card["max_hp"]
    assert normalized["armor_class"] == 10
    assert normalized["ruleset_character"]["locale"] == "zh-CN"


def test_normalize_rejects_incompatible_binding(runtime, rule) -> None:
    card = runtime.finalize_character(rule, _draft())
    card["ruleset_character"]["rule_binding"]["content_version"] = "someone-else-9"
    with pytest.raises(ValueError):
        runtime.normalize_character_submission(rule, card, "zh-CN")


def test_normalize_rejects_missing_binding(runtime, rule) -> None:
    card = runtime.finalize_character(rule, _draft())
    del card["ruleset_character"]["rule_binding"]
    with pytest.raises(ValueError):
        runtime.normalize_character_submission(rule, card, "zh-CN")


def test_normalize_is_idempotent(runtime, rule) -> None:
    once = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, _draft()), "zh-CN",
    )
    twice = runtime.normalize_character_submission(rule, once, "zh-CN")
    assert once == twice


def test_normalize_accepts_bare_client_card(runtime, rule) -> None:
    """建卡器只回传 draft 形状时也要能归一化。"""

    normalized = runtime.normalize_character_submission(rule, _draft(), "zh-CN")
    assert normalized["ruleset_character"]["rule_binding"]["runtime_id"] == "custom:declarative"
    assert normalized["ruleset_character"]["mechanics"]["resources"]


def test_project_legacy_accepts_both_shapes(runtime, rule) -> None:
    canonical = runtime.derive_character(rule, _draft())
    direct = runtime.project_legacy_character(canonical)
    wrapped = runtime.project_legacy_character(
        {"rule_binding": canonical["rule_binding"], "ruleset_character": canonical},
    )
    assert direct == wrapped
    assert direct["race"] == "人类"
    assert direct["attributes"] == BALANCED


# ---------------------------------------------------------------------------
# 阶段 0 的验收：两道门槛变自动
# ---------------------------------------------------------------------------
def test_join_binds_runtime_and_seeds_declaration(runtime, rule, tmp_path) -> None:
    """建卡流程走完之后：存档被绑定、规则声明进了 ruleset_state、资源已派发。"""

    _registry, instance = _game(tmp_path)
    card = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, _draft()), "zh-CN",
    )
    _join(instance, card, runtime=runtime)

    # 门槛②：绑定
    assert instance.ruleset_runtime["id"] == "custom:declarative"
    assert instance.ruleset_runtime["content_version"] == card["rule_binding"]["content_version"]
    # 门槛③：规则声明快照 —— 游戏期读规则全靠它
    state = instance.ruleset_state
    assert state["mechanics"]["resources"][0]["id"] == "resolve"
    assert state["mechanics"]["checks"][0]["id"] == "will_check"
    # 资源已按声明派发（initial = {"constant": 50}）
    assert state["players"][UID]["resources"] == {"resolve": 50}


def test_gameplay_reads_declaration_from_seat_sheet(runtime, rule, tmp_path) -> None:
    """老存档没有 ``state["mechanics"]`` 时从席位角色卡自愈，且**只读不改存档**。"""

    _registry, instance = _game(tmp_path, "p2")
    card = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, _draft()), "zh-CN",
    )
    instance.players[UID] = {
        "character_name": "自愈者",
        "character_sheet": {"ruleset_character": deepcopy(card["ruleset_character"])},
    }
    assert "mechanics" not in instance.ruleset_state

    intents = runtime.available_intents(instance, UID)
    assert {item["type"] for item in intents} == {"custom.check.roll"}
    assert "will_check" in [item["check_id"] for item in intents]

    # 读路径不能在写锁之外改状态
    assert "mechanics" not in instance.ruleset_state


def test_validate_intent_rejects_client_supplied_roll(runtime, rule, tmp_path) -> None:
    """客户端只能声明"想做什么"，不能声明"掷出了什么"。

    ``Natural 20 不能改写现实`` 从实现纪律变成协议强制的地方。
    """

    _registry, instance = _game(tmp_path, "p4")
    card = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, _draft()), "zh-CN",
    )
    _join(instance, card, runtime=runtime)

    clean = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": "will_check"}, UID, False,
    )
    assert runtime.validate_intent(instance, clean)["ok"] is True

    forged = dict(clean, d20=20, total=25)
    verdict = runtime.validate_intent(instance, forged)
    assert verdict["ok"] is False
    assert verdict["code"] == "CLIENT_ROLL_FORBIDDEN"
    assert "d20" in verdict["error"]


def test_apply_event_batch_fails_closed_on_unknown_event(runtime, rule, tmp_path) -> None:
    """不认识的事件必须让整批失败，**不能静默丢弃权威变更**。

    静默跳过会让"玩家看到球火生效了，slot 没扣"这类 bug 变得极难查——
    而且它会直接破坏“全部成功或全部不发生”。
    """

    _registry, instance = _game(tmp_path, "p5")
    card = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, _draft()), "zh-CN",
    )
    _join(instance, card, runtime=runtime)

    with pytest.raises(ValueError, match="不支持的事件类型"):
        runtime.apply_event_batch(
            instance, {"events": [{"type": "dnd2024.combat.started"}]},
        )
    with pytest.raises(ValueError, match="非对象事件"):
        runtime.apply_event_batch(instance, {"events": ["nope"]})
    with pytest.raises(ValueError, match="未声明的资源"):
        runtime.apply_event_batch(instance, {"events": [{
            "type": "custom.resource.changed", "actor_id": UID,
            "resource_id": "mana", "delta": -1,
        }]})


def test_stage_b_pipeline_needs_no_manual_seed(runtime, rule, tmp_path) -> None:
    """建卡 + 加入之后，权威意图应当直接可用（不再手工 ``seed_rule_snapshot``）。"""

    _registry, instance = _game(tmp_path, "p3")
    card = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, _draft()), "zh-CN",
    )
    _join(instance, card, runtime=runtime)

    intents = runtime.available_intents(instance, UID)
    assert "will_check" in [item["check_id"] for item in intents]

    intent = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": "will_check"}, UID, False,
    )
    assert runtime.validate_intent(instance, intent)["ok"] is True

    resolved = runtime.resolve_intent(instance, intent, __import__("random").SystemRandom())
    assert resolved["ok"] is True
    applied = runtime.apply_event_batch(instance, resolved["event_batch"])
    assert applied.get("applied") is True
    # 裁定记录必须被保留 —— 叙事层靠它读掷骰与成功度，不能静默丢弃。
    assert applied["recorded_events"], "裁定记录没被 reducer 保留"
    recorded = applied["recorded_events"][0]
    assert recorded["type"] == "custom.check.resolved"
    # 掷骰与成功度必须一起留下来 —— 叙事层要靠它决定"怎么讲"。
    assert recorded["degree"]
    assert recorded["degree_label"]
    assert recorded["target"] is not None
    # 掷骰结果随机，只断言落在声明区间内、事件账本有记录。
    resources = instance.ruleset_state["players"][UID]["resources"]
    assert 0 <= resources["resolve"] <= 99
    assert instance.event_ledger
    assert instance.event_ledger[-1]["recorded"][0]["type"] == "custom.check.resolved"
