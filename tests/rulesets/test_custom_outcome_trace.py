"""结果分解 + 后果严重度 + 规则追踪。

这三样的共同目标：让"为什么是这样"变成**数据**，而不是只能靠读代码猜。

- **结果分解**把"部分成功"从一个魔法枚举拆成两个问题（成没成 / 成了多少）；
- **严重度**与"风险"分成两个轴，避免同一个原因被计两遍；
- **追踪**记录这一次裁定里哪条规则为什么触发了。

前两样是给规则作者的，第三样是给玩家、GM 模型和排错用的。
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
    ConsequenceSeverity,
    GoalOutcome,
    Risk,
    RuleTrace,
    SeverityScale,
    TRACE_KINDS,
    severity_for_degree,
    steps_below_success,
)
from src.rulesets.contracts import RulesetCapabilities
from src.rulesets.custom.manifest import parse_custom_mechanics
from src.rulesets.custom.runtime import CustomDeclarativeRuntime

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "templates" / "rules"
RULE_ID = "custom_freeform"
UID = "trace_player"


class _FixedRng:
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
    instance = registry.get_or_create(("web", "outcome", key))
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


def _submit(runtime, instance, check_id: str, value: int) -> dict:
    intent = runtime.prepare_intent_submission(
        {"type": "custom.check.roll", "check_id": check_id}, UID, False,
    )
    resolved = runtime.resolve_intent(instance, intent, _FixedRng(value))
    assert resolved["ok"] is True, resolved
    return runtime.apply_event_batch(instance, resolved["event_batch"])


def _record(result: dict) -> dict:
    return result["recorded_events"][0]


# ---------------------------------------------------------------------------
# 1. 目标达成：成没成 / 成了多少
# ---------------------------------------------------------------------------
def test_partial_is_derived_not_a_degree_name() -> None:
    """"部分成功"不是一个枚举值，而是"达成 + 程度在开区间内"。"""

    assert GoalOutcome(achieved=True, extent=0.5).partial is True
    assert GoalOutcome(achieved=False, extent=1.0).partial is False, "没达成谈不上部分"
    assert GoalOutcome(achieved=True, extent=1.0).partial is False
    assert GoalOutcome(achieved=True, extent=0.0).partial is False


def test_steps_below_success_counts_from_the_worst_success() -> None:
    """"差几档"从**最差的成功档**往上数。0 就是成功，不管它叫什么名字。"""

    # 从好到坏：opened(成) / ajar(成) / jammed(败)
    order = (("opened", True), ("ajar", True), ("jammed", False))
    assert steps_below_success(order, "opened") == 0
    assert steps_below_success(order, "ajar") == 0
    assert steps_below_success(order, "jammed") == 1


def test_steps_below_success_never_goes_negative() -> None:
    """全部档位都未达成时，计数从 1 起 —— **不能倒扣成负数**，那会让严重度下降。"""

    order = (("nothing", False), ("worse", False))
    best = steps_below_success(order, "nothing")
    worst = steps_below_success(order, "worse")

    assert best >= 0
    assert worst > best, "越差的档位级数越大"


def test_unknown_degree_id_does_not_produce_a_negative_step() -> None:
    assert steps_below_success((("a", True), ("b", False)), "missing") == 0


# ---------------------------------------------------------------------------
# 2. 严重度与风险是两个轴
# ---------------------------------------------------------------------------
def test_severity_scale_clamps_at_both_ends() -> None:
    scale = SeverityScale(base=ConsequenceSeverity.MINOR, per_step=1)
    assert scale.raise_by(0) is ConsequenceSeverity.MINOR
    assert scale.raise_by(2) is ConsequenceSeverity.SEVERE
    assert scale.raise_by(99) is ConsequenceSeverity.SEVERE, "不越界"
    assert scale.raise_by(-5) is ConsequenceSeverity.MINOR, "不倒扣"


def test_severity_scale_per_step_zero_pins_the_base() -> None:
    """``per_step=0`` = 严重度恒定，与差几档无关。"""

    scale = SeverityScale(base=ConsequenceSeverity.MAJOR, per_step=0)
    assert scale.raise_by(0) is ConsequenceSeverity.MAJOR
    assert scale.raise_by(3) is ConsequenceSeverity.MAJOR


def test_severity_base_lands_on_the_shallowest_failure() -> None:
    """1 档 = 刚好失败 = base。不减这 1，``base`` 就永远不会被用到。"""

    scale = SeverityScale(base=ConsequenceSeverity.MINOR, per_step=1)
    assert scale.resolve(degree_steps_below=0) is ConsequenceSeverity.MINOR
    assert scale.resolve(degree_steps_below=1) is ConsequenceSeverity.MINOR
    assert scale.resolve(degree_steps_below=2) is ConsequenceSeverity.MAJOR


def test_success_has_no_consequence_severity() -> None:
    """成功了就没有"后果"可言 —— 这跟风险是两个完全不同的轴。"""

    order = (("good", True), ("bad", False))
    scale = SeverityScale(base=ConsequenceSeverity.SEVERE, per_step=2)
    assert severity_for_degree("good", order, scale) is ConsequenceSeverity.NONE


def test_risk_never_moves_the_severity() -> None:
    """同一个原因不能被计两遍：风险动掷骰难度，严重度动后果量级。"""

    order = (("good", True), ("bad", False))
    scale = SeverityScale(base=ConsequenceSeverity.MINOR, per_step=1)
    expected = severity_for_degree("bad", order, scale)
    assert expected is ConsequenceSeverity.MINOR, "刚好失败 = base"

    for risk in (None, Risk.LOW, Risk.STANDARD, Risk.HIGH, Risk.EXTREME):
        assert severity_for_degree("bad", order, scale, risk=risk) is expected


# ---------------------------------------------------------------------------
# 3. 追踪
# ---------------------------------------------------------------------------
def test_trace_rejects_an_unknown_kind() -> None:
    """追踪阶段词汇保持很窄 —— 条目一多就没人看了。"""

    with pytest.raises(ValueError, match="未知的追踪阶段"):
        RuleTrace().add("x", "not_a_phase")


def test_trace_is_immutable_and_ordered() -> None:
    trace = RuleTrace().add("a", "pre_roll", "first").add("b", "roll", "second")

    assert [entry.rule for entry in trace.entries] == ["a", "b"]
    assert len(trace) == 2
    assert trace.of_kind("roll")[0].reason == "second"
    assert RuleTrace().of_kind("roll") == ()
    assert not RuleTrace()


def test_trace_entries_are_json_shaped() -> None:
    trace = RuleTrace().add("a", "roll", "why", target=30)
    assert trace.to_list() == [
        {"rule": "a", "kind": "roll", "reason": "why", "detail": {"target": 30}},
    ]


# ---------------------------------------------------------------------------
# 4. 声明期
# ---------------------------------------------------------------------------
def _parse(raw: dict, degrees) -> object:
    template = deepcopy(raw)
    checks = template["custom_mechanics"]["checks"]
    next(c for c in checks if c["id"] == "pick_lock")["degrees"] = degrees
    return parse_custom_mechanics(template)


_OPENED = {"id": "opened", "label": "打开", "max_ratio": 0.5}


def test_declaration_derives_goal_from_structure(raw_rule) -> None:
    mechanics = parse_custom_mechanics(raw_rule)
    check = next(c for c in mechanics.checks if c.id == "pick_lock")

    by_id = {degree.id: degree for degree in check.degrees}
    assert by_id["opened"].goal_achieved is True
    assert by_id["opened"].goal_extent == 1.0
    assert by_id["ajar"].goal_extent == 0.5, "显式声明的程度"
    assert by_id["jammed"].goal_achieved is False, "兜底档 = 失败档"
    assert by_id["jammed"].goal_extent == 0.0


def test_declaration_rejects_an_out_of_range_extent(raw_rule) -> None:
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError, match="extent 必须在"):
            _parse(raw_rule, [_OPENED, {"id": "bad", "label": "x",
                                        "max_ratio": 1.0, "extent": bad, "fallback": True}])


def test_declaration_rejects_a_fallback_that_claims_success_without_extent(
    raw_rule,
) -> None:
    """兜底档说"达成了"却不给程度，会拿到派生默认的 0.0 —— 自相矛盾。"""

    with pytest.raises(ValueError, match="必须同时给出 extent"):
        _parse(raw_rule, [
            _OPENED,
            {"id": "jammed", "label": "卡住", "fallback": True, "achieved": True},
        ])


def test_declaration_accepts_a_fallback_that_gains_something(raw_rule) -> None:
    """"失败但仍有收获"是合法的，只要写明程度。"""

    mechanics = _parse(raw_rule, [
        _OPENED,
        {"id": "jammed", "label": "卡住", "fallback": True,
         "achieved": True, "extent": 0.25},
    ])
    check = next(c for c in mechanics.checks if c.id == "pick_lock")
    assert check.degrees[-1].goal_extent == 0.25


def test_declaration_rejects_an_unknown_severity(raw_rule) -> None:
    template = deepcopy(raw_rule)
    checks = template["custom_mechanics"]["checks"]
    next(c for c in checks if c["id"] == "pick_lock")["adjudication"] = {
        "severity": "CATASTROPHIC",
    }
    with pytest.raises(ValueError, match="severity 必须是"):
        parse_custom_mechanics(template)


def test_declaration_rejects_a_negative_severity_step(raw_rule) -> None:
    template = deepcopy(raw_rule)
    checks = template["custom_mechanics"]["checks"]
    next(c for c in checks if c["id"] == "will_check")["adjudication"] = {
        "severity_per_step": -1,
    }
    with pytest.raises(ValueError, match="不能为负"):
        parse_custom_mechanics(template)


def test_checks_without_a_scale_yield_no_severity(raw_rule) -> None:
    """不声明 = 不产生严重度，而且**不会因为差得多就自己往上涨**。"""

    mechanics = parse_custom_mechanics(raw_rule)
    check = next(c for c in mechanics.checks if c.id == "will_check")
    assert check.severity.base is ConsequenceSeverity.NONE
    assert check.severity.per_step == 0
    assert check.severity.raise_by(9) is ConsequenceSeverity.NONE


# ---------------------------------------------------------------------------
# 5. 端到端：结果分解真的落在事件里
# ---------------------------------------------------------------------------
def test_success_reports_a_full_goal(runtime, rule, tmp_path) -> None:
    instance = _ready(runtime, rule, tmp_path, "goal_ok")
    outcome = _record(_submit(runtime, instance, "pick_lock", 20))["outcome"]

    assert outcome["degree"] == "opened"
    assert outcome["goal"] == {"achieved": True, "extent": 1.0, "partial": False}
    assert outcome["severity"] == "NONE", "成功档差 0 级"
    assert outcome["costs"] == [{
        "resource": "resolve", "amount": 3,
        "timing": "ON_SUCCESS", "reason": "消耗：撬锁",
    }]


def test_partial_degree_reports_an_extent(runtime, rule, tmp_path) -> None:
    """``ajar`` 只声明了 ``extent: 0.5`` —— 没有发明新档位名，也没有改宿主。"""

    instance = _ready(runtime, rule, tmp_path, "goal_partial")
    outcome = _record(_submit(runtime, instance, "pick_lock", 30))["outcome"]

    assert outcome["degree"] == "ajar"
    assert outcome["goal"] == {"achieved": True, "extent": 0.5, "partial": True}
    assert outcome["costs"][0]["amount"] == 3, "部分成功也算成功，照样扣"


def test_failure_reports_severity_scaled_by_how_far_it_missed(
    runtime, rule, tmp_path,
) -> None:
    instance = _ready(runtime, rule, tmp_path, "goal_fail")
    outcome = _record(_submit(runtime, instance, "pick_lock", 90))["outcome"]

    assert outcome["goal"] == {"achieved": False, "extent": 0.0, "partial": False}
    assert outcome["severity"] == "MINOR", "基础 MINOR + 差 1 档 × 1"
    assert outcome["costs"] == [], "ON_SUCCESS 在失败时不扣"


def test_costs_reflect_what_was_actually_charged(runtime, rule, tmp_path) -> None:
    """代价账记的是**事实**：ON_DECLARE 在短路路径上也出现。"""

    instance = _ready(runtime, rule, tmp_path, "costs_declare")
    record = _record(_submit(runtime, instance, "break_wall", 50))

    assert record["resolution"] == "IMPOSSIBLE"
    assert "outcome" not in record, "没掷骰就没有成功度，不编一个"
    assert [entry["kind"] for entry in record["trace"]].count("consumption") == 1


# ---------------------------------------------------------------------------
# 6. 端到端：追踪链
# ---------------------------------------------------------------------------
def test_trace_walks_from_target_to_degree(runtime, rule, tmp_path) -> None:
    instance = _ready(runtime, rule, tmp_path, "trace_full")
    trace = _record(_submit(runtime, instance, "pick_lock", 20))["trace"]

    assert [entry["kind"] for entry in trace] == ["roll", "degree", "consumption"]
    assert trace[0]["rule"] == "check.target"
    assert trace[0]["detail"]["target"] == 40
    assert trace[1]["detail"]["severity"] == "NONE"
    assert trace[2]["detail"]["charged"] is True
    assert trace[2]["detail"]["timing"] == "ON_SUCCESS"


def test_trace_explains_a_pre_roll_short_circuit(runtime, rule, tmp_path) -> None:
    """短路时追踪必须说清楚**是哪条声明**让它不掷骰的。"""

    instance = _ready(runtime, rule, tmp_path, "trace_short")
    trace = _record(_submit(runtime, instance, "break_wall", 50))["trace"]

    kinds = [entry["kind"] for entry in trace]
    assert set(kinds) <= set(TRACE_KINDS)
    short = next(
        entry for entry in trace if entry["rule"] == "adjudication.can_succeed"
    )
    assert short["kind"] == "pre_roll"
    assert short["detail"]["resolution"] == "IMPOSSIBLE"
    assert kinds.count("consumption") == 1, "ON_DECLARE 在短路路径上仍然扣"
    assert all(entry["reason"] for entry in trace), "每条都要有理由"


def test_trace_records_the_effective_target(runtime, rule, tmp_path) -> None:
    """目标数来自有效值 —— 追踪把当时的资源快照一并留下，才查得出来。"""

    instance = _ready(runtime, rule, tmp_path, "trace_effective")
    _submit(runtime, instance, "will_check", 75)  # 失败 → 挂上 -5 的效果 + 1d6

    trace = _record(_submit(runtime, instance, "will_check", 75))["trace"]
    target = next(entry for entry in trace if entry["rule"] == "check.target")

    assert target["detail"]["resources"]["resolve"] == 39, "44 基础 − 5 效果"


def test_trace_is_narrow_enough_to_read(runtime, rule, tmp_path) -> None:
    """一次正常裁定不该产出几十条追踪 —— 那样没人会看。"""

    instance = _ready(runtime, rule, tmp_path, "trace_narrow")
    trace = _record(_submit(runtime, instance, "will_check", 1))["trace"]
    assert len(trace) <= 5
