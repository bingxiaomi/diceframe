"""裁定契约（``src/rulesets/adjudication.py``）的单元测试。

这一层是纯数据 + 纯函数，所以测试也是纯的：不需要服务、不需要 LLM、
不需要前端。重点验三件事：

1. **控制流词汇保持小** —— 宿主只需要 5 个值 + 派生信号；
2. **硬约束真的是硬约束** —— 客户端提供骰子字段会被拒；非法裁定构造不出来；
3. **往返不掉信息** —— ``to_event`` / ``from_event`` 是同一份数据的两种视图。
"""

from __future__ import annotations

import pytest

from src.rulesets.adjudication import (
    ACTIVITY_TYPES,
    CONSUMPTION_FAILED,
    EFFECT_CHANGE_OPS,
    Adjudication,
    AdjudicationError,
    Activity,
    Competence,
    CompetenceLevel,
    Effect,
    EffectDescriptor,
    OutcomeDegree,
    Resolution,
    Risk,
    Stakes,
    check_resolved_event,
    intent_field_violations,
    normalize_degree,
    three_question_resolution,
)

# ---------------------------------------------------------------------------
# 1. 控制流词汇
# ---------------------------------------------------------------------------
def test_resolution_host_signals() -> None:
    assert Resolution.CHECK_REQUIRED.requires_check is True
    assert Resolution.AUTO_SUCCESS.requires_check is False
    assert Resolution.NEEDS_CLARIFICATION.needs_clarification is True
    assert Resolution.IMPOSSIBLE.needs_clarification is False
    assert Resolution.AUTO_SUCCESS.outcome_sign == 1
    assert Resolution.AUTO_FAILURE.outcome_sign == -1
    assert Resolution.IMPOSSIBLE.outcome_sign == -1
    assert Resolution.CHECK_REQUIRED.outcome_sign == 0


def test_resolution_vocabulary_stays_small() -> None:
    """多加控制流档位前先问：宿主会走不同分支吗？这条例行公事地守住它。"""

    assert len(Resolution) == 5, "控制流词汇膨胀了，先想清楚宿主怎么用"


# ---------------------------------------------------------------------------
# 2. 三问法
# ---------------------------------------------------------------------------
def test_three_questions_auto_success_when_cannot_fail() -> None:
    """打开一扇没锁的门：不该掷骰。"""

    assert three_question_resolution(
        can_succeed=True, can_fail=False, failure_matters=True,
    ) is Resolution.AUTO_SUCCESS


def test_three_questions_auto_success_when_failure_does_not_matter() -> None:
    """无限时间反复开锁：骰子没有意义，直接给成功，同时堵掉"我再试一次"。"""

    assert three_question_resolution(
        can_succeed=True, can_fail=True, failure_matters=False,
    ) is Resolution.AUTO_SUCCESS


def test_three_questions_impossible() -> None:
    """徒手推倒城墙。"""

    assert three_question_resolution(
        can_succeed=False, can_fail=True, failure_matters=True,
    ) is Resolution.IMPOSSIBLE


def test_three_questions_check_required() -> None:
    """火灾里 20 秒撬锁：可能成、可能败、失败有意义。"""

    assert three_question_resolution(
        can_succeed=True, can_fail=True, failure_matters=True,
    ) is Resolution.CHECK_REQUIRED


# ---------------------------------------------------------------------------
# 3. 结果档位映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "expected"), [
    ("CRITICAL_SUCCESS", OutcomeDegree.CRITICAL_SUCCESS),
    ("critical_success", OutcomeDegree.CRITICAL_SUCCESS),
    ("extreme", OutcomeDegree.CRITICAL_SUCCESS),      # 我们 d100 的"极难"
    ("hard", OutcomeDegree.SUCCESS),                  # 我们 d100 的"困难"
    ("fumble", OutcomeDegree.CRITICAL_FAILURE),
    ("Partial Success", OutcomeDegree.PARTIAL_SUCCESS),
    ("success_with_cost", OutcomeDegree.PARTIAL_SUCCESS),
])
def test_normalize_degree_aliases(raw: str, expected: OutcomeDegree) -> None:
    assert normalize_degree(raw) is expected


def test_normalize_degree_refuses_to_guess() -> None:
    """认不出必须返回 None —— 猜错会把失败写成成功。"""

    assert normalize_degree("banana") is None
    assert normalize_degree("") is None
    assert normalize_degree(None) is None


def test_degree_severity_and_success() -> None:
    assert OutcomeDegree.PARTIAL_SUCCESS.succeeded is True
    assert OutcomeDegree.FAILURE.succeeded is False
    assert OutcomeDegree.CRITICAL_SUCCESS.severity > OutcomeDegree.SUCCESS.severity
    assert OutcomeDegree.FAILURE.severity > OutcomeDegree.CRITICAL_FAILURE.severity


# ---------------------------------------------------------------------------
# 4. 风险 / 效果：环境决定风险，而不是角色数值
# ---------------------------------------------------------------------------
def test_risk_encodes_environment_not_stats() -> None:
    """同样的角色：窄巷 STANDARD，一人打五个 EXTREME。"""

    assert Risk.STANDARD.dc_modifier < Risk.EXTREME.dc_modifier
    assert Risk.STANDARD.grants_disadvantage is False
    assert Risk.HIGH.grants_disadvantage is True


def test_effect_shifts_degree() -> None:
    assert Effect.LIMITED.degree_shift < 0
    assert Effect.GREAT.degree_shift > 0
    assert Effect.STANDARD.degree_shift == 0
    assert Effect.NONE.degree_shift < Effect.LIMITED.degree_shift


# ---------------------------------------------------------------------------
# 5. 效果描述符
# ---------------------------------------------------------------------------
def _resource_effect(**overrides) -> EffectDescriptor:
    payload = {
        "kind": "resource", "target": "pc_01", "field": "resolve",
        "op": "subtract", "value": "1d6",
    }
    payload.update(overrides)
    return EffectDescriptor(**payload)


def test_effect_descriptor_accepts_foundry_vocabulary() -> None:
    assert set(EFFECT_CHANGE_OPS) == {"add", "subtract", "multiply", "override", "upgrade", "downgrade"}
    for op in EFFECT_CHANGE_OPS:
        _resource_effect(op=op).validate()


@pytest.mark.parametrize(("overrides", "message"), [
    ({"kind": "nonsense"}, "未知的效果类别"),
    ({"op": "divide"}, "未知的效果算子"),
    ({"target": ""}, "缺少 target"),
    ({"field": "  "}, "缺少 field"),
    ({"duration": {"type": "eon"}}, "未知的持续时间类型"),
])
def test_effect_descriptor_rejects_garbage(overrides: dict, message: str) -> None:
    with pytest.raises(AdjudicationError, match=message):
        _resource_effect(**overrides).validate()


def test_effect_descriptor_carries_provenance() -> None:
    """没有 source 就回答不了"这个 +1d4 是哪来的、什么时候到期"。"""

    effect = _resource_effect(source="intent:abc", duration={"type": "round", "remaining": 8})
    assert effect.source == "intent:abc"
    effect.validate()
    assert effect.to_dict()["duration"] == {"type": "round", "remaining": 8}


# ---------------------------------------------------------------------------
# 6. Stakes
# ---------------------------------------------------------------------------
def test_stakes_pick_effects_by_degree() -> None:
    stakes = Stakes(
        success=(_resource_effect(op="add", value="1"),),
        partial=(_resource_effect(op="subtract", value="1"),),
        failure=(_resource_effect(op="subtract", value="1d6"),),
    )
    stakes.validate()
    assert stakes.for_degree(OutcomeDegree.SUCCESS) == stakes.success
    assert stakes.for_degree(OutcomeDegree.PARTIAL_SUCCESS) == stakes.partial
    assert stakes.for_degree(OutcomeDegree.FAILURE) == stakes.failure
    assert stakes.for_degree(OutcomeDegree.CRITICAL_FAILURE) == stakes.failure
    assert stakes.for_degree(None) == ()


def test_stakes_describe_is_human_readable() -> None:
    """叙事层与玩家看的是人话，不是 JSON。"""

    stakes = Stakes(failure=(_resource_effect(),))
    lines = stakes.describe()
    assert lines and "失败" in lines[0] and "resolve" in lines[0]


def test_stakes_reject_non_descriptors() -> None:
    with pytest.raises(AdjudicationError, match="非效果描述符"):
        Stakes(success=("not-an-effect",)).validate()  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. Activity
# ---------------------------------------------------------------------------
def test_activity_types_cover_foundry_activities() -> None:
    for expected in ("check", "attack", "save", "damage", "heal", "cast", "summon", "teleport", "transform"):
        assert expected in ACTIVITY_TYPES


def test_activity_rejects_unknown_type_and_missing_actor() -> None:
    with pytest.raises(AdjudicationError, match="未知的动作类型"):
        Activity(type="telepathy", actor_id="pc_01").validate()
    with pytest.raises(AdjudicationError, match="缺少 actor_id"):
        Activity(type="check", actor_id="").validate()


def test_activity_needs_roll_classification() -> None:
    assert Activity(type="check", actor_id="pc_01").needs_roll is True
    assert Activity(type="attack", actor_id="pc_01").needs_roll is True
    assert Activity(type="damage", actor_id="pc_01").needs_roll is False
    assert Activity(type="use", actor_id="pc_01").needs_roll is False


def test_activity_has_no_client_roll_field() -> None:
    """骰子结果不属于活动声明 —— 客户端不许提供。"""

    assert "roll" not in Activity.__slots__
    assert "d20" not in Activity.__slots__


# ---------------------------------------------------------------------------
# 8. 完整裁定的校验
# ---------------------------------------------------------------------------
def _check_adjudication(**overrides) -> Adjudication:
    payload = {
        "resolution": Resolution.CHECK_REQUIRED,
        "goal": "进入被锁住的仓库",
        "approach": "用短剑插进门缝撬锁扣",
        "activity": Activity(
            type="check", actor_id="player:pc_01", ability="str", skill="athletics", dc=14,
        ),
        "risk": Risk.HIGH,
        "effect_level": Effect.LIMITED,
        "reason": ("门结构允许暴力撬动", "短剑并非合适工具"),
    }
    payload.update(overrides)
    return Adjudication(**payload)


def test_valid_adjudication_passes() -> None:
    _check_adjudication().validate()


def test_check_required_demands_an_activity() -> None:
    """CHECK_REQUIRED 却不给 activity → 宿主不知道掷什么。"""

    with pytest.raises(AdjudicationError, match="必须给出 activity"):
        Adjudication(resolution=Resolution.CHECK_REQUIRED, reason=("因为",)).validate()


def test_check_required_demands_a_target_number() -> None:
    with pytest.raises(AdjudicationError, match="dc / skill / ability"):
        _check_adjudication(activity=Activity(type="check", actor_id="player:pc_01")).validate()


def test_auto_resolution_must_not_carry_rolling_activity() -> None:
    """AUTO_SUCCESS 配 attack 是自相矛盾：那还需要掷骰。"""

    with pytest.raises(AdjudicationError, match="不该带需要掷骰的 activity"):
        _check_adjudication(resolution=Resolution.AUTO_SUCCESS).validate()


def test_auto_failure_may_carry_non_rolling_activity() -> None:
    _check_adjudication(
        resolution=Resolution.AUTO_FAILURE,
        activity=Activity(type="utility", actor_id="player:pc_01"),
    ).validate()


def test_reason_is_mandatory_for_real_verdicts() -> None:
    """没有 reason 叙事层无从解释，玩家只会看到"系统说不行"。"""

    with pytest.raises(AdjudicationError, match="至少一条 reason"):
        _check_adjudication(reason=()).validate()


def test_clarification_is_exempt_from_reason() -> None:
    Adjudication(
        resolution=Resolution.NEEDS_CLARIFICATION, goal="做点什么",
    ).validate()


# ---------------------------------------------------------------------------
# 9. 事件往返
# ---------------------------------------------------------------------------
def test_event_roundtrip_keeps_everything() -> None:
    original = _check_adjudication(
        competence=Competence(
            level=CompetenceLevel.PROFICIENT,
            ability_modifier=4,
            tool="thieves_tools",
            evidence=("背景：盗贼",),
        ),
        stakes=Stakes(
            success=(_resource_effect(op="override", value="true", field="door_open"),),
            failure=(_resource_effect(op="subtract", value="1d6"),),
        ),
    )
    restored = Adjudication.from_event(original.to_event())
    assert restored == original
    assert restored.to_event() == original.to_event()


def test_event_payload_uses_existing_event_type() -> None:
    """用既有的 check.resolved 承载，不新造事件外壳。"""

    payload = check_resolved_event(_check_adjudication(), source="intent:abc")
    assert payload["type"] == "check.resolved"
    assert payload["resolution"] == "CHECK_REQUIRED"
    assert payload["activity"]["skill"] == "athletics"
    assert payload["source"] == "intent:abc"


def test_from_event_rejects_unknown_resolution() -> None:
    with pytest.raises(AdjudicationError, match="未知的 resolution"):
        Adjudication.from_event({"resolution": "MAYBE", "reason": ["x"]})


def test_with_degree_and_roll_are_immutable_updates() -> None:
    base = _check_adjudication()
    rolled = base.with_degree("extreme")
    assert base.degree is None, "原对象不能被就地修改"
    assert rolled.degree is OutcomeDegree.CRITICAL_SUCCESS

    stamped = rolled.with_roll({"d100": 7, "total": 7})
    assert stamped.roll == {"d100": 7, "total": 7}
    assert rolled.roll is None


def test_with_degree_rejects_unknown_label() -> None:
    with pytest.raises(AdjudicationError, match="无法识别的结果档位"):
        _check_adjudication().with_degree("banana")


# ---------------------------------------------------------------------------
# 10. 硬约束：客户端不许提供骰子结果
# ---------------------------------------------------------------------------
def test_intent_field_violations_flags_roll_fields() -> None:
    violations = intent_field_violations({
        "type": "custom.check.roll",
        "check_id": "will_check",
        "d20": 20,
        "total": 25,
    })
    assert violations == ["d20", "total"]


def test_intent_field_violations_looks_inside_nested_payloads() -> None:
    """藏在嵌套结构里也要被抓到。"""

    violations = intent_field_violations({
        "type": "attack",
        "payload": {"weapon": {"damage": {"roll": "2d6"}}},
        "targets": [{"id": "npc_1", "result": "hit"}],
    })
    assert "payload.weapon.damage.roll" in violations
    assert "targets[0].result" in violations


def test_intent_field_violations_allows_legitimate_fields() -> None:
    assert intent_field_violations({
        "type": "custom.check.roll",
        "check_id": "will_check",
        "actor_id": "player:pc_01",
        "expected_version": 3,
        "target": "door_01",
    }) == []


def test_consumption_failure_code_exists() -> None:
    """资源不够要让裁定失败，不是静默跳过（对齐 Foundry 的 ConsumptionError）。"""

    assert CONSUMPTION_FAILED == "CONSUMPTION_FAILED"
