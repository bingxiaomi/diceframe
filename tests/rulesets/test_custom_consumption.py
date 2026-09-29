"""消耗时机：代价在哪个点上被扣，以及"扣不起"时会发生什么。

三种时机的差别**玩家能直接感知到**，所以必须用测试把它们分开：

- ``ON_DECLARE``：就算检定被判定为不可能、连骰子都没掷，代价也照付；
- ``ON_RESOLVE``：掷了就付，成或败都付；
- ``ON_SUCCESS``：成了才付，失败时资源留着。

在"整批要么全落、要么全不落"的原子架构下，ON_DECLARE 与 ON_RESOLVE 唯一
可观测的差别就是**短路路径**（骰前裁定为 IMPOSSIBLE/AUTO_*，不掷骰）。
本文件把这个差别钉住，免得两个时机退化成同一个。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.engine.game_instance import GameRegistry
from src.rules.loader import RuleBundleLoader
from src.rules.rule_system import RuleSystem
from src.rulesets.adjudication import (
    CONSUMPTION_FAILED,
    ConsumptionTiming,
    consumption_charges,
    consumption_checked_before_roll,
)
from src.rulesets.contracts import RulesetCapabilities
from src.rulesets.custom.manifest import parse_custom_mechanics
from src.rulesets.custom.runtime import CustomDeclarativeRuntime

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "templates" / "rules"
RULE_ID = "custom_freeform"
UID = "consume_player"

#: ``break_wall`` 声明为 can_succeed=false → 骰前就判 IMPOSSIBLE，永远不掷骰。
IMPOSSIBLE_CHECK = "break_wall"
IMPOSSIBLE_COST = 5
#: ``pick_lock`` 会真的掷骰：opened / jammed（兜底）。
SUCCESS_CHECK = "pick_lock"
SUCCESS_COST = 3
SUCCESS_TARGET = 40


class _FixedRng:
    """固定骰值，夹到合法区间（同一颗骰子要供 1d100 与 1d6 共用）。"""

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


def _ready(runtime, rule, tmp_path: Path, key: str):
    registry = GameRegistry(tmp_path / "saves")
    instance = registry.get_or_create(("web", "consume", key))
    instance.world_id = "test_fantasy"
    instance.rule_id = RULE_ID
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


def _resolve(runtime, instance, check_id: str, value: int) -> dict:
    intent = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": check_id}, UID, False,
    )
    return runtime.resolve_intent(instance, intent, _FixedRng(value))


def _submit(runtime, instance, check_id: str, value: int) -> dict:
    resolved = _resolve(runtime, instance, check_id, value)
    assert resolved["ok"] is True, resolved
    return runtime.apply_event_batch(instance, resolved["event_batch"])


def _base(runtime, instance, resource_id: str = "resolve") -> int:
    return int(instance.ruleset_state["players"][UID]["resources"][resource_id])


# ---------------------------------------------------------------------------
# 1. 契约本身（纯函数）
# ---------------------------------------------------------------------------
def test_charges_table_separates_the_three_timings() -> None:
    """三种时机在 (rolled, succeeded) 两个维度上必须**两两可分**。

    如果它们会塌成同一个值，那声明这个字段就没有意义 —— 而玩家能感知的
    差别也就消失了。
    """

    cases = {
        # (rolled, succeeded): 该扣的时机集合
        (False, False): {ConsumptionTiming.ON_DECLARE},
        (True, False): {ConsumptionTiming.ON_DECLARE, ConsumptionTiming.ON_RESOLVE},
        (True, True): set(ConsumptionTiming),
    }
    for (rolled, succeeded), expected in cases.items():
        charged = {
            timing for timing in ConsumptionTiming
            if consumption_charges(timing, rolled=rolled, succeeded=succeeded)
        }
        assert charged == expected, f"rolled={rolled} succeeded={succeeded}"


def test_success_timing_is_never_checked_before_the_roll() -> None:
    """ON_SUCCESS 不能在骰前拦截 —— 骰前拦截等于把它变成 ON_RESOLVE。"""

    assert consumption_checked_before_roll(ConsumptionTiming.ON_DECLARE) is True
    assert consumption_checked_before_roll(ConsumptionTiming.ON_RESOLVE) is True
    assert consumption_checked_before_roll(ConsumptionTiming.ON_SUCCESS) is False


# ---------------------------------------------------------------------------
# 2. 声明期 fail-fast
# ---------------------------------------------------------------------------
def _parse(raw: dict, entries) -> object:
    template = deepcopy(raw)
    template["custom_mechanics"]["consumptions"] = entries
    return parse_custom_mechanics(template)


_OK = {"check": SUCCESS_CHECK, "resource": "resolve", "amount": 3,
       "timing": "ON_RESOLVE"}


@pytest.mark.parametrize("amount", [0, -5, 2.5, True, "3", None])
def test_declaration_rejects_a_non_positive_integer_amount(raw_rule, amount) -> None:
    """零和负数都不是"消耗"。负消耗其实是奖励，那是 effects 的活。"""

    with pytest.raises(ValueError, match="amount 必须是正整数"):
        _parse(raw_rule, [{**_OK, "amount": amount}])


def test_declaration_rejects_an_unknown_timing(raw_rule) -> None:
    with pytest.raises(ValueError, match="timing 必须是"):
        _parse(raw_rule, [{**_OK, "timing": "ON_CRIT"}])


def test_declaration_rejects_an_undeclared_resource(raw_rule) -> None:
    with pytest.raises(ValueError, match="未声明的资源"):
        _parse(raw_rule, [{**_OK, "resource": "mana"}])


def test_declaration_rejects_an_undeclared_check(raw_rule) -> None:
    with pytest.raises(ValueError, match="未声明的检定"):
        _parse(raw_rule, [{**_OK, "check": "climb_wall"}])


def test_declaration_rejects_a_consumption_that_can_never_fire(raw_rule) -> None:
    """把 ON_SUCCESS 挂在永远不掷骰的检定上是**死声明**，加载时就拒。

    这是我自己写示例规则时踩到的：search_room 声明了 failure_matters=false，
    骰前就短路成 AUTO_SUCCESS，于是"成功才消耗"永远不会发生。规则作者在
    跑团时只会看到"我写的消耗从来没扣过"，而且根本查不出来为什么。
    """

    for check_id in ("search_room", "break_wall"):
        with pytest.raises(ValueError, match="永远不会触发"):
            _parse(raw_rule, [{**_OK, "check": check_id, "timing": "ON_SUCCESS"}])

    # ON_DECLARE 不依赖成功度，在同一个检定上是合法的。
    parsed = _parse(raw_rule, [{**_OK, "check": "break_wall", "timing": "ON_DECLARE"}])
    assert parsed.consumptions[0].timing == "ON_DECLARE"


def test_consult_attempts_is_not_treated_as_never_rolling(raw_rule) -> None:
    """``consult_attempts`` 是**有条件**短路（看账本），仍会掷骰，不算死声明。

    把它一起拒掉会把一个合法的规则写法误判成错误。
    """

    template = deepcopy(raw_rule)
    checks = template["custom_mechanics"]["checks"]
    next(c for c in checks if c["id"] == SUCCESS_CHECK)["adjudication"] = {
        "consult_attempts": True,
    }
    template["custom_mechanics"]["consumptions"] = [
        {**_OK, "timing": "ON_RESOLVE"},
    ]

    mechanics = parse_custom_mechanics(template)
    assert mechanics.consumptions[0].timing == "ON_RESOLVE"


def test_declaration_accepts_the_declared_shape(raw_rule) -> None:
    mechanics = _parse(raw_rule, [_OK])
    spec = mechanics.consumptions_for(SUCCESS_CHECK)[0]
    assert (spec.resource, spec.amount, spec.timing) == ("resolve", 3, "ON_RESOLVE")
    assert mechanics.consumptions_for("will_check") == ()


def test_failure_degree_is_derived_not_declared(runtime, rule) -> None:
    """失败档从分级末尾**派生**，不是另一处声明 —— 两处声明会漂移。"""

    mechanics = parse_custom_mechanics(
        json.loads((RULES_DIR / f"{RULE_ID}.json").read_text(encoding="utf-8")),
    )
    by_id = {check.id: check for check in mechanics.checks}
    assert by_id["will_check"].failure_degree_id == "failure"
    assert by_id[SUCCESS_CHECK].failure_degree_id == "jammed"


# ---------------------------------------------------------------------------
# 3. ON_DECLARE：连骰子都没掷也照付
# ---------------------------------------------------------------------------
def test_declare_timing_charges_even_when_the_check_is_impossible(
    runtime, rule, tmp_path,
) -> None:
    """``break_wall`` 骰前就被判 IMPOSSIBLE。代价照样付 —— "我试了"发生了。"""

    instance = _ready(runtime, rule, tmp_path, "declare")
    before = _base(runtime, instance)

    result = _submit(runtime, instance, IMPOSSIBLE_CHECK, 50)

    assert _base(runtime, instance) == before - IMPOSSIBLE_COST
    # 没有掷骰：批次里只有消耗 + 一条裁定记录。
    assert [event["type"] for event in result["events"]] == ["custom.resource.changed"]
    recorded = result["recorded_events"]
    assert [event["resolution"] for event in recorded] == ["IMPOSSIBLE"]


def test_declare_timing_refuses_when_the_cost_is_unaffordable(
    runtime, rule, tmp_path,
) -> None:
    """扣不起就直接拒绝，**不掷骰** —— 否则玩家先看到结果，再被告知"其实不行"。"""

    instance = _ready(runtime, rule, tmp_path, "broke")
    base = _base(runtime, instance)
    affordable = base // IMPOSSIBLE_COST

    # 必须真的落盘才会扣钱 —— resolve_intent 本身不写状态。
    for _ in range(affordable):
        _submit(runtime, instance, IMPOSSIBLE_CHECK, 50)

    assert _base(runtime, instance) == base - affordable * IMPOSSIBLE_COST

    refused = _resolve(runtime, instance, IMPOSSIBLE_CHECK, 50)
    assert refused["ok"] is False
    assert refused["code"] == CONSUMPTION_FAILED
    assert "resolve" in refused["error"]
    # 拒绝不留痕：余额不变，也不会变成负数。
    assert _base(runtime, instance) == base - affordable * IMPOSSIBLE_COST
    assert _base(runtime, instance) >= 0


def test_declare_timing_survives_a_zero_balance_without_going_negative(
    runtime, rule, tmp_path,
) -> None:
    """反复提交直到扣光：最后一次的余额必须正好是 0，不是 -3。"""

    instance = _ready(runtime, rule, tmp_path, "zero")
    for _ in range(_base(runtime, instance) // IMPOSSIBLE_COST):
        _submit(runtime, instance, IMPOSSIBLE_CHECK, 50)

    assert _base(runtime, instance) == 0
    assert _resolve(runtime, instance, IMPOSSIBLE_CHECK, 50)["code"] == CONSUMPTION_FAILED


def test_consumption_reason_is_recorded(runtime, rule, tmp_path) -> None:
    instance = _ready(runtime, rule, tmp_path, "reason")
    before = _base(runtime, instance)
    result = _submit(runtime, instance, IMPOSSIBLE_CHECK, 50)

    # 落盘后的资源事件记的是 before/after（审计友好），不是意图里的 delta。
    event = result["events"][0]
    assert (event["before"], event["after"]) == (before, before - IMPOSSIBLE_COST)
    assert event["reason"] == "消耗：硬撼"


# ---------------------------------------------------------------------------
# 4. ON_SUCCESS：失败不扣，而且不被余额拦住
# ---------------------------------------------------------------------------
def test_success_timing_does_not_charge_on_failure(runtime, rule, tmp_path) -> None:
    instance = _ready(runtime, rule, tmp_path, "miss")
    before = _base(runtime, instance)

    roll = SUCCESS_TARGET + 20  # 60/40 = 1.5 → 落到兜底的 jammed
    _submit(runtime, instance, SUCCESS_CHECK, roll)

    assert _base(runtime, instance) == before


def test_success_timing_charges_on_success(runtime, rule, tmp_path) -> None:
    instance = _ready(runtime, rule, tmp_path, "found")
    before = _base(runtime, instance)

    roll = SUCCESS_TARGET // 2  # 20/40 = 0.5 → opened
    _submit(runtime, instance, SUCCESS_CHECK, roll)

    assert _base(runtime, instance) == before - SUCCESS_COST


def test_success_timing_is_not_gated_by_the_balance(runtime, rule, tmp_path) -> None:
    """余额为 0 也允许掷骰 —— 失败本来就不用付。"""

    instance = _ready(runtime, rule, tmp_path, "empty")
    instance.ruleset_state["players"][UID]["resources"]["resolve"] = 0

    for _ in range(3):
        roll = SUCCESS_TARGET + 20
        assert _resolve(runtime, instance, SUCCESS_CHECK, roll)["ok"] is True

    assert _base(runtime, instance) == 0


# ---------------------------------------------------------------------------
# 5. 多笔消耗先加起来再比
# ---------------------------------------------------------------------------
def test_costs_on_the_same_resource_are_summed_before_comparing(
    runtime, rule, tmp_path,
) -> None:
    """两笔各 5、手上只有 8：分开比会都放行，加起来才是正确的判断。"""

    instance = _ready(runtime, rule, tmp_path, "sum")
    instance.ruleset_state["players"][UID]["resources"]["resolve"] = 8
    mechanics = parse_custom_mechanics(json.loads(
        (RULES_DIR / f"{RULE_ID}.json").read_text(encoding="utf-8"),
    ))
    spec = mechanics.consumptions_for(IMPOSSIBLE_CHECK)[0]

    assert runtime._unaffordable(instance, UID, (spec,)) is None
    assert runtime._unaffordable(instance, UID, (spec, spec)) == ("resolve", 10, 8)


def test_reward_is_not_swallowed_by_a_full_resource(
    runtime, rule, tmp_path,
) -> None:
    """消耗排在奖励前面：否则奖励先被上限截掉一段，再扣就白亏了。

    ``will_check`` 的 extreme 档奖励 +1 resolve，同一检定还带 ON_DECLARE 类
    消耗时，顺序决定了净结果。这里直接验证**同一批次内**先扣后加。
    """

    instance = _ready(runtime, rule, tmp_path, "order")
    template = json.loads(
        (RULES_DIR / f"{RULE_ID}.json").read_text(encoding="utf-8"),
    )
    # will_check: extreme 奖励 +1；这里再挂一笔 5 的即时消耗。
    template["custom_mechanics"]["consumptions"] = [
        {"check": "will_check", "resource": "resolve", "amount": 5,
         "timing": "ON_RESOLVE", "label": "发力"},
    ]
    instance.ruleset_state["mechanics"] = template["custom_mechanics"]
    before = _base(runtime, instance)

    result = _submit(runtime, instance, "will_check", 1)  # extreme

    resource_events = [
        event for event in result["events"]
        if event["type"] == "custom.resource.changed"
    ]
    signed = [event["after"] - event["before"] for event in resource_events]
    assert signed == [-5, 1], "消耗必须在奖励之前"
    assert _base(runtime, instance) == before - 5 + 1
