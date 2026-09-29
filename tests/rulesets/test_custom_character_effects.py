"""``custom:declarative`` 的角色效果端到端：声明 → 事件 → 账本 → 投影。

这条链路补的是"成功了之后角色的数值怎么变"：

- 效果**不写基础值**，只往账本里加一条带来源与时长的修正量；
- 有效值在读取时算（base + effects），所以到期不需要回滚代码；
- 有效值**真的参与检定取目标数** —— 否则"挂上了"只是账面漂移。

最后一条是重点。只把效果存进账本不算闭环；它必须能改变下一次裁定的结果，
否则玩家看不到任何差别，规则作者也不会发现写错了。
"""

from __future__ import annotations

import json
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
UID = "effect_player"

#: 失败档：100/50 = 2.0，落进 fallback。
FAIL_ROLL = 75
#: 临界档：45/50 = 0.90 → 成功；45/39 = 1.15 → 失败。同一颗骰子，两种裁定。
EDGE_ROLL = 45


class _FixedRng:
    """每次 ``randint`` 返回固定值，并**夹到合法区间内**。

    夹取是必要的：``resolve_intent`` 用同一颗骰子掷 ``1d100`` 与 ``-1d6``。
    不夹取的话固定值 75 会让 1d6 掷出 75，把资源打到 0，测试就不再是在测效果。
    """

    def __init__(self, value: int) -> None:
        self.value = value

    def randint(self, low: int, high: int) -> int:
        return max(low, min(high, self.value))


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


@pytest.fixture(scope="module")
def raw_rule() -> dict:
    return json.loads((RULES_DIR / f"{RULE_ID}.json").read_text(encoding="utf-8"))


def _game(tmp_path: Path, key: str):
    registry = GameRegistry(tmp_path / "saves")
    instance = registry.get_or_create(("web", "effects", key))
    instance.world_id = "test_fantasy"
    instance.rule_id = RULE_ID
    return registry, instance


def _ready(runtime, rule, tmp_path: Path, key: str):
    _registry, instance = _game(tmp_path, key)
    card = runtime.normalize_character_submission(
        rule,
        runtime.finalize_character(rule, {"attributes": {
            "str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10,
        }}),
        "zh-CN",
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


def _submit(runtime, instance, value: int, check_id: str = "will_check") -> dict:
    intent = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": check_id}, UID, False,
    )
    resolved = runtime.resolve_intent(instance, intent, _FixedRng(value))
    assert resolved["ok"] is True, resolved
    return runtime.apply_event_batch(instance, resolved["event_batch"])


def _degree(runtime, instance, value: int) -> str:
    intent = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": "will_check"}, UID, False,
    )
    batch = runtime.resolve_intent(instance, intent, _FixedRng(value))["event_batch"]
    rolled = [e for e in batch["events"] if e["type"] == "custom.check.resolved"]
    assert len(rolled) == 1
    return str(rolled[0]["degree"])


def _base(runtime, instance) -> dict[str, int]:
    return dict(instance.ruleset_state["players"][UID]["resources"])


# ---------------------------------------------------------------------------
# 1. 声明 → 账本
# ---------------------------------------------------------------------------
def test_failure_attaches_an_active_effect(runtime, rule, tmp_path) -> None:
    """失败在角色身上留下痕迹，而且痕迹带着来源。"""

    instance = _ready(runtime, rule, tmp_path, "attach")
    assert runtime.active_effects(instance, UID) == []

    _submit(runtime, instance, FAIL_ROLL)

    effects = runtime.active_effects(instance, UID)
    assert len(effects) == 1
    assert effects[0]["id"] == "will_shaken"
    assert effects[0]["source"] == "意志检定:失败"
    assert effects[0]["duration"] == {"type": "round", "remaining": 2}


def test_effect_is_reported_as_a_state_change(runtime, rule, tmp_path) -> None:
    """效果是权威状态变更，不是旁注 —— ``changed_state`` 必须为真。"""

    instance = _ready(runtime, rule, tmp_path, "changed")
    result = _submit(runtime, instance, FAIL_ROLL)

    assert result["changed_state"] is True
    kinds = {event["type"] for event in result["events"]}
    assert "custom.effect.applied" in kinds


# ---------------------------------------------------------------------------
# 2. 基础值 vs 有效值
# ---------------------------------------------------------------------------
def test_effect_never_writes_the_base_value(runtime, rule, tmp_path) -> None:
    """效果**只**影响有效值。基础值原封不动是这条链路的核心保证。"""

    instance = _ready(runtime, rule, tmp_path, "base")
    base_before = _base(runtime, instance)

    _submit(runtime, instance, FAIL_ROLL)

    base_after = _base(runtime, instance)
    effective = runtime.effective_resources(instance, UID)

    assert base_after["resolve"] == base_before["resolve"] - 6, "只有 1d6 该动基础值"
    assert effective["resolve"] == base_after["resolve"] - 5, "效果走的是有效值"
    assert effective["resolve"] < base_after["resolve"]


def test_expiring_restores_the_value_without_any_rollback(
    runtime, rule, tmp_path,
) -> None:
    """到期后有效值自己回去 —— 因为没有"加回去"这段代码。"""

    instance = _ready(runtime, rule, tmp_path, "expire")
    _submit(runtime, instance, FAIL_ROLL)
    base = _base(runtime, instance)
    assert runtime.effective_resources(instance, UID)["resolve"] == base["resolve"] - 5

    first = runtime.expire_effects(instance, "round")
    assert first == []
    assert runtime.effective_resources(instance, UID)["resolve"] == base["resolve"] - 5

    second = runtime.expire_effects(instance, "round")
    assert [effect["id"] for effect in second] == ["will_shaken"]
    assert runtime.active_effects(instance, UID) == []
    assert runtime.effective_resources(instance, UID) == base


# ---------------------------------------------------------------------------
# 3. 有效值真的参与裁定
# ---------------------------------------------------------------------------
def test_effect_shifts_the_next_checks_degree(runtime, rule, tmp_path) -> None:
    """同一颗骰子，挂上效果前是成功，挂上后是失败。

    这条是整条链路的存在意义：如果投影只影响展示，不影响取目标数，
    那"效果生效了"就只是账面漂移。
    """

    clean = _ready(runtime, rule, tmp_path, "clean")
    assert _degree(runtime, clean, EDGE_ROLL) == "success"

    shaken = _ready(runtime, rule, tmp_path, "shaken")
    _submit(runtime, shaken, FAIL_ROLL)
    effective = runtime.effective_resources(shaken, UID)
    assert effective["resolve"] == 39, "44 基础 − 5 效果"

    assert _degree(runtime, shaken, EDGE_ROLL) == "failure"


# ---------------------------------------------------------------------------
# 4. 同 id 是续期
# ---------------------------------------------------------------------------
def test_same_effect_id_refreshes_instead_of_stacking(
    runtime, rule, tmp_path,
) -> None:
    """重复施加同一个效果不会叠成 −10：它只是续期。

    叠层会让"这个 −10 哪来的"无法回答，而且第二次失败的惩罚会与第一次
    绑定，惩罚强度变成失败次数的函数 —— 那不是规则作者声明的东西。
    """

    instance = _ready(runtime, rule, tmp_path, "refresh")
    _submit(runtime, instance, FAIL_ROLL)

    for _ in range(2):
        _submit(runtime, instance, FAIL_ROLL)

    effects = runtime.active_effects(instance, UID)
    assert len(effects) == 1
    assert effects[0]["duration"] == {"type": "round", "remaining": 2}, "续期到满"
    # 连续失败三次之后效果仍是**一条 −5**。基础值确实每次都掉（那是 1d6
    # 资源效果，另一套机制），所以拿当下的基础值比对，而不是第一次的。
    assert len(effects[0]["changes"]) == 1
    assert effects[0]["changes"][0]["value"] == 5
    fresh_base = _base(runtime, instance)
    assert runtime.effective_resources(instance, UID)["resolve"] == fresh_base["resolve"] - 5


# ---------------------------------------------------------------------------
# 5. 声明期 fail-fast
# ---------------------------------------------------------------------------
def _parse(raw: dict, entries) -> object:
    template = deepcopy(raw)
    template["custom_mechanics"]["character_effects"] = entries
    return parse_custom_mechanics(template)


_GOOD_CHANGES = [{"key": "resources.resolve", "op": "subtract", "value": 5}]


def test_declaration_rejects_an_unknown_operator(raw_rule) -> None:
    with pytest.raises(ValueError, match="op 必须是"):
        _parse(raw_rule, [{
            "check": "will_check", "degree": "failure", "id": "x",
            "changes": [{"key": "resources.resolve", "op": "divide", "value": 2}],
        }])


def test_declaration_rejects_writing_presentation(raw_rule) -> None:
    """呈现层不接受效果 —— 描述不是权威数值。"""

    with pytest.raises(ValueError, match="写不进 presentation"):
        _parse(raw_rule, [{
            "check": "will_check", "degree": "failure", "id": "x",
            "changes": [{"key": "presentation.description", "op": "override", "value": "x"}],
        }])


def test_declaration_rejects_an_undeclared_degree(raw_rule) -> None:
    with pytest.raises(ValueError, match="未声明的成功度"):
        _parse(raw_rule, [{
            "check": "will_check", "degree": "critical_success", "id": "x",
            "changes": _GOOD_CHANGES,
        }])


def test_declaration_rejects_an_unknown_duration_kind(raw_rule) -> None:
    with pytest.raises(ValueError, match="duration.type 必须是"):
        _parse(raw_rule, [{
            "check": "will_check", "degree": "failure", "id": "x",
            "changes": _GOOD_CHANGES, "duration": {"type": "forever", "remaining": 1},
        }])


def test_declaration_rejects_an_empty_change_list(raw_rule) -> None:
    with pytest.raises(ValueError, match="changes 必须是非空数组"):
        _parse(raw_rule, [{
            "check": "will_check", "degree": "failure", "id": "x", "changes": [],
        }])


def test_declaration_accepts_a_permanent_effect(raw_rule) -> None:
    """``duration`` 省略 = 永久。这不是错误，是明确的语义。"""

    mechanics = _parse(raw_rule, [{
        "check": "will_check", "degree": "failure", "id": "x", "changes": _GOOD_CHANGES,
    }])
    assert mechanics.character_effects[0].duration is None


# ---------------------------------------------------------------------------
# 6. 归约器 fail-closed
# ---------------------------------------------------------------------------
def test_reducer_rejects_an_effect_without_changes(runtime, rule, tmp_path) -> None:
    """坏事件必须炸，不能被静默丢掉 —— 丢掉的权威变更查不出来。"""

    instance = _ready(runtime, rule, tmp_path, "bad")
    batch = {
        "batch_id": "b1",
        "intent_type": "custom.check.roll",
        "actor_id": f"player:{UID}",
        "events": [{
            "type": "custom.effect.applied", "actor_id": UID,
            "effect_id": "x", "changes": [],
        }],
    }
    with pytest.raises(ValueError, match="缺少 changes"):
        runtime.apply_event_batch(instance, batch)


def test_reducer_rejects_an_unwritable_change(runtime, rule, tmp_path) -> None:
    """声明期过不了的字段，运行期也一样过不了（同一套校验）。"""

    instance = _ready(runtime, rule, tmp_path, "bad2")
    batch = {
        "batch_id": "b2",
        "intent_type": "custom.check.roll",
        "actor_id": f"player:{UID}",
        "events": [{
            "type": "custom.effect.applied", "actor_id": UID, "effect_id": "x",
            "changes": [{"key": "presentation.description", "op": "override", "value": "x"}],
        }],
    }
    with pytest.raises(ValueError):
        runtime.apply_event_batch(instance, batch)


# ---------------------------------------------------------------------------
# 7. 投影给前端 / LLM
# ---------------------------------------------------------------------------
def _own_seat(view: dict) -> dict:
    return next(seat for seat in view["seats"] if seat["is_self"])


def test_gameplay_view_serves_the_effective_value_with_its_base(
    runtime, rule, tmp_path,
) -> None:
    """界面拿到的是有效值，同时知道底子在哪 —— 才能显示"44 → 39"。"""

    instance = _ready(runtime, rule, tmp_path, "view")
    _submit(runtime, instance, FAIL_ROLL)

    seat = _own_seat(runtime.gameplay_view(instance, viewer_id=UID))
    resolve = seat["resources"]["resolve"]

    assert resolve["value"] == resolve["base"] - 5
    assert [effect["id"] for effect in seat["effects"]] == ["will_shaken"]
    assert seat["effects"][0]["source"] == "意志检定:失败"
    assert seat["effects"][0]["duration"] == {"type": "round", "remaining": 2}


def test_view_shape_is_untouched_when_nothing_is_active(
    runtime, rule, tmp_path,
) -> None:
    """没有效果时不长出新字段 —— 绝大多数席位是这种情况，别让载荷平白变胖。"""

    instance = _ready(runtime, rule, tmp_path, "plain")

    seat = _own_seat(runtime.gameplay_view(instance, viewer_id=UID))
    assert "effects" not in seat
    resolve = seat["resources"]["resolve"]
    assert "base" not in resolve
    assert resolve["value"] == instance.ruleset_state["players"][UID]["resources"]["resolve"]


def test_other_seats_do_not_leak_effects(runtime, rule, tmp_path) -> None:
    """效果和资源一样属于隐私边界：非 GM 看别人的席位只得到摘要。"""

    instance = _ready(runtime, rule, tmp_path, "privacy")
    _submit(runtime, instance, FAIL_ROLL)
    rival = "rival_player"
    instance.players[rival] = {"character_name": "对手"}
    instance.ruleset_state.setdefault("players", {})[rival] = {
        "resources": {"resolve": 50},
    }

    view = runtime.gameplay_view(instance, viewer_id=UID)
    other = next(seat for seat in view["seats"] if seat["player_id"] == rival)

    assert "effects" not in other
    assert "resources" not in other
    assert other["resource_count"] == 1

    gm_view = runtime.gameplay_view(instance, viewer_id=UID, viewer_is_gm=True)
    gm_other = next(seat for seat in gm_view["seats"] if seat["player_id"] == rival)
    assert gm_other["resources"]["resolve"]["value"] == 50


def test_llm_view_carries_effective_values_and_a_warning(
    runtime, rule, tmp_path,
) -> None:
    """GM 模型必须看到有效值，并且明确被告知不要去写这些数字。"""

    instance = _ready(runtime, rule, tmp_path, "llm")
    _submit(runtime, instance, FAIL_ROLL)

    authority = runtime.build_llm_view(instance)["ruleset_authority"]
    seat = authority["seats"][UID]

    assert seat["resources"]["resolve"]["value"] == seat["resources"]["resolve"]["base"] - 5
    assert [effect["id"] for effect in seat["effects"]] == ["will_shaken"]
    assert "Never write these numbers" in authority["effective_values"]


def test_llm_view_omits_effects_when_there_are_none(runtime, rule, tmp_path) -> None:
    instance = _ready(runtime, rule, tmp_path, "llm_plain")

    authority = runtime.build_llm_view(instance)["ruleset_authority"]
    assert "effects" not in authority["seats"][UID]
    assert "base" not in authority["seats"][UID]["resources"]["resolve"]
