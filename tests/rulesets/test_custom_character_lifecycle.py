"""``custom:declarative`` 的专业建卡（阶段 0）：绑定与规则快照必须**自动**发生。

阶段 0 的目标不是"多一个建卡器"，而是让上一轮发现的两道手工门槛变成自动的：

    门槛②  存档被绑定（``bind_ruleset_runtime``）
    门槛③  规则声明进了 ``ruleset_state["mechanics"]``

做法是打开 ``character_builder="professional"`` +
``character_lifecycle="rules_aware"``。但默认能力位**不能**跟着打开 ——
``describe_experience`` 会返回 ``profile="custom"``，而前端注册表里还没有对应的
建卡组件，打开会让入局 / 建房页报 ``Unsupported ruleset experience``。

所以这里用"继承真 runtime、只覆盖 ``capabilities``"的写法（范本：
``tests/rulesets/test_dnd2024_m5_http.py``），既测到专业分支，又不动发布默认值。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from src.engine.game_instance import GameRegistry
from src.rules.loader import RuleBundleLoader
from src.rules.rule_system import RuleSystem
from src.rulesets.contracts import RulesetCapabilities
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
# 默认能力位必须保持关闭
# ---------------------------------------------------------------------------
def test_default_capabilities_stay_off() -> None:
    """发布默认值不能被测试带偏，否则前端会坏。"""

    capabilities = CustomDeclarativeRuntime.capabilities
    assert capabilities.character_builder == "guided"
    assert capabilities.character_lifecycle == "legacy"


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
    assert [item["type"] for item in intents] == ["custom.check.roll"]
    assert [item["check_id"] for item in intents] == ["will_check"]

    # 读路径不能在写锁之外改状态
    assert "mechanics" not in instance.ruleset_state


def test_stage_b_pipeline_needs_no_manual_seed(runtime, rule, tmp_path) -> None:
    """建卡 + 加入之后，权威意图应当直接可用（不再手工 ``seed_rule_snapshot``）。"""

    _registry, instance = _game(tmp_path, "p3")
    card = runtime.normalize_character_submission(
        rule, runtime.finalize_character(rule, _draft()), "zh-CN",
    )
    _join(instance, card, runtime=runtime)

    intents = runtime.available_intents(instance, UID)
    assert [item["check_id"] for item in intents] == ["will_check"]

    intent = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": "will_check"}, UID, False,
    )
    assert runtime.validate_intent(instance, intent)["ok"] is True

    resolved = runtime.resolve_intent(instance, intent, __import__("random").SystemRandom())
    assert resolved["ok"] is True
    applied = runtime.apply_event_batch(instance, resolved["event_batch"])
    assert applied.get("applied") is True
    # 掷骰结果随机，只断言落在声明区间内、事件账本有记录。
    resources = instance.ruleset_state["players"][UID]["resources"]
    assert 0 <= resources["resolve"] <= 99
    assert instance.event_ledger
