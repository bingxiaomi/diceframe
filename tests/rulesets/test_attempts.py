"""尝试记录层（``src/rulesets/attempts.py``）的单元测试。

纯数据 + 纯函数。重点验四件事：

1. **换手段 = 换 key** —— 撬锁失败天然不挡"拿斧头砍门"，不需要特判；
2. **失效靠版本，不靠手工 reset** —— 而且只比较记录里出现过的目标；
3. **版本要记"落完之后"的** —— 记错时刻会让记录自我作废 → 无限重试（下面有反例）；
4. **只有 FREE 才让失败不留痕** —— 这是三问法 ``failure_matters`` 的唯一来源。
"""

from __future__ import annotations

import pytest

from src.rulesets.adjudication import OutcomeDegree, three_question_resolution
from src.rulesets.attempts import (
    MAX_ATTEMPTS,
    AttemptError,
    RetryPolicy,
    attempt_applies,
    attempt_key,
    failure_sticks,
    find_prior_failure,
    load_attempts,
    record_attempt,
    retry_verdict,
    world_versions,
)

ACTOR = "player:pc_1"
DOOR = "door_01"
FAMILY = "unlock"

LOCKPICK = "lockpick"
FORCE = "force"


def _door(version: int = 8) -> dict:
    return {DOOR: {"kind": "door", "state": {"locked": True}, "version": version}}


def _ledger_with(
    *, policy: RetryPolicy | str = RetryPolicy.REQUIRES_CHANGED_CIRCUMSTANCE,
    outcome: OutcomeDegree | str = OutcomeDegree.FAILURE,
    approach: str = LOCKPICK,
    door_version: int = 8,
) -> tuple[list[dict], dict[str, int]]:
    """记一条"撬锁失败"的记录，返回 ``(ledger, 记录时的版本快照)``。"""

    ledger: list[dict] = []
    versions = world_versions(_door(door_version), [DOOR])
    record_attempt(
        ledger, actor_id=ACTOR, outcome=outcome, intent_family=FAMILY,
        target_id=DOOR, approach_signature=approach,
        retry_policy=policy, world_versions=versions,
        consequence=["lock_jammed"],
    )
    return ledger, versions


# ---------------------------------------------------------------------------
# 1. key
# ---------------------------------------------------------------------------
def test_key_separates_approaches() -> None:
    """goal 相同、approach 不同 → 完全不同的尝试。"""

    assert attempt_key(ACTOR, DOOR, FAMILY, LOCKPICK) != attempt_key(ACTOR, DOOR, FAMILY, FORCE)
    assert attempt_key(ACTOR, DOOR, FAMILY, LOCKPICK) == attempt_key(ACTOR, DOOR, FAMILY, LOCKPICK)


def test_key_distinguishes_actor_and_target() -> None:
    assert attempt_key("a", DOOR, FAMILY, LOCKPICK) != attempt_key("b", DOOR, FAMILY, LOCKPICK)
    assert attempt_key(ACTOR, "door_02", FAMILY, LOCKPICK) != attempt_key(ACTOR, DOOR, FAMILY, LOCKPICK)


def test_key_keeps_empty_segments_distinct() -> None:
    """``a||b`` 与 ``a|b|`` 不是同一件事 —— 空片段不能被吞掉。"""

    assert attempt_key("a", "", "b") != attempt_key("a", "b", "")


# ---------------------------------------------------------------------------
# 2. 记录
# ---------------------------------------------------------------------------
def test_record_normalizes_degree_and_policy() -> None:
    ledger, _ = _ledger_with(outcome="failure", policy="REQUIRES_CHANGED_CIRCUMSTANCE")
    entry = ledger[0]
    assert entry["outcome"] == "FAILURE"
    assert entry["retry_policy"] == "REQUIRES_CHANGED_CIRCUMSTANCE"
    assert entry["consequence"] == ["lock_jammed"]
    assert entry["key"] == attempt_key(ACTOR, DOOR, FAMILY, LOCKPICK)


def test_record_accepts_runtime_degree_labels() -> None:
    """规则集自己的档位标签（extreme / fumble）也能记。"""

    ledger: list[dict] = []
    record_attempt(
        ledger, actor_id=ACTOR, outcome="extreme", intent_family=FAMILY,
    )
    assert ledger[0]["outcome"] == str(OutcomeDegree.CRITICAL_SUCCESS)


@pytest.mark.parametrize(("overrides", "message"), [
    ({"actor_id": ""}, "缺少 actor_id"),
    ({"intent_family": "  "}, "缺少 intent_family"),
    ({"outcome": "banana"}, "无法识别的结果档位"),
    ({"retry_policy": "WHATEVER"}, "未知的重试策略"),
])
def test_record_rejects_garbage(overrides: dict, message: str) -> None:
    payload = {
        "actor_id": ACTOR, "outcome": OutcomeDegree.FAILURE, "intent_family": FAMILY,
    }
    payload.update(overrides)
    with pytest.raises(AttemptError, match=message):
        record_attempt([], **payload)


def test_record_keeps_ledger_bounded() -> None:
    ledger: list[dict] = []
    for index in range(MAX_ATTEMPTS + 10):
        record_attempt(ledger, actor_id=ACTOR, outcome=OutcomeDegree.FAILURE,
                       intent_family=FAMILY, note=str(index))
    assert len(ledger) == MAX_ATTEMPTS
    assert ledger[-1]["note"] == str(MAX_ATTEMPTS + 9), "保留最近的历史"


def test_load_attempts_validates_shape() -> None:
    assert load_attempts(None) == []
    assert load_attempts([]) == []
    with pytest.raises(AttemptError, match="必须是数组"):
        load_attempts({"not": "a list"})
    with pytest.raises(AttemptError, match=r"attempts\[0\] 必须是对象"):
        load_attempts(["nope"])


# ---------------------------------------------------------------------------
# 3. 失效判定：只比较记录里出现过的目标
# ---------------------------------------------------------------------------
def test_attempt_applies_only_compares_recorded_targets() -> None:
    """别的对象变了不影响这条记录。"""

    ledger, versions = _ledger_with(door_version=8)
    assert attempt_applies(ledger[0], versions) is True
    assert attempt_applies(ledger[0], {**versions, "chest_01": 99}) is True
    assert attempt_applies(ledger[0], {DOOR: 9}) is False


def test_attempt_without_versions_always_applies() -> None:
    assert attempt_applies({"key": "x"}, {DOOR: 9}) is True


def test_vanished_target_counts_as_changed() -> None:
    ledger, _ = _ledger_with(door_version=8)
    assert attempt_applies(ledger[0], {}) is False


def test_world_versions_skips_missing_objects() -> None:
    """引用不存在的对象会让记录永远不适用 —— 那是静默失效，不如不记。"""

    assert world_versions(_door(), ["door_01", "ghost"]) == {DOOR: 8}
    assert world_versions({}, ["ghost"]) == {}


# ---------------------------------------------------------------------------
# 4. 换手段 = 换 key（你的 撬锁 vs 斧头 场景）
# ---------------------------------------------------------------------------
def test_different_approach_is_not_blocked_by_prior_failure() -> None:
    """撬锁失败**不该**阻止"我拿斧头砍门"。"""

    ledger, versions = _ledger_with(policy=RetryPolicy.REQUIRES_CHANGED_APPROACH)

    same = retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    )
    assert same.allowed is False
    assert same.has_history is True

    other = retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=FORCE, world_versions=versions,
    )
    assert other.allowed is True
    assert other.has_history is False, "换了手段就是另一个 key，查不到记录"
    assert "首次尝试" in other.reason or "环境已变化" in other.reason


# ---------------------------------------------------------------------------
# 5. 失效：GM 改世界 → 自动可重试，不需要手工 reset
# ---------------------------------------------------------------------------
def test_gm_relocking_allows_retry_without_manual_reset() -> None:
    ledger, versions = _ledger_with(policy=RetryPolicy.REQUIRES_CHANGED_CIRCUMSTANCE)
    blocked = retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    )
    assert blocked.allowed is False

    # GM 重新锁门 → 版本 8 → 9。没有调用任何 clear_attempts()。
    assert retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions={DOOR: 9},
    ).allowed is True


def test_recording_pre_attempt_version_would_allow_infinite_retries() -> None:
    """**反例**：如果记的是"尝试前"的版本，记录会把自己作废。

    失败的后果本身改了目标（``lock_jammed`` → 版本 7→8）。此时：
      - 记"落完之后"的 8 → 下次（当前 8）仍然适用 → 正确拦住
      - 记"尝试前"的 7   → 下次（当前 8）判定"世界变了" → 无限重试 ✗
    """

    after = 8

    correct: list[dict] = []
    record_attempt(correct, actor_id=ACTOR, outcome=OutcomeDegree.FAILURE,
                   intent_family=FAMILY, target_id=DOOR, approach_signature=LOCKPICK,
                   retry_policy=RetryPolicy.REQUIRES_CHANGED_CIRCUMSTANCE,
                   world_versions={DOOR: after})
    assert retry_verdict(
        correct, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions={DOOR: after},
    ).allowed is False

    wrong: list[dict] = []
    record_attempt(wrong, actor_id=ACTOR, outcome=OutcomeDegree.FAILURE,
                   intent_family=FAMILY, target_id=DOOR, approach_signature=LOCKPICK,
                   retry_policy=RetryPolicy.REQUIRES_CHANGED_CIRCUMSTANCE,
                   world_versions={DOOR: 7})
    assert retry_verdict(
        wrong, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions={DOOR: after},
    ).allowed is True, "记错时刻的后果：无限重试"


# ---------------------------------------------------------------------------
# 6. 只看失败；成功不构成阻碍
# ---------------------------------------------------------------------------
def test_success_does_not_count_as_prior_failure() -> None:
    ledger, versions = _ledger_with(
        policy=RetryPolicy.FORBIDDEN, outcome=OutcomeDegree.SUCCESS,
    )
    assert find_prior_failure(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    ) is None
    assert retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    ).allowed is True


def test_finds_the_latest_applicable_failure() -> None:
    ledger = [
        {"key": attempt_key(ACTOR, DOOR, FAMILY, LOCKPICK),
         "actor_id": ACTOR, "intent_family": FAMILY, "approach_signature": LOCKPICK,
         "outcome": "FAILURE", "retry_policy": "FREE", "world_versions": {DOOR: 5}},
        {"key": attempt_key(ACTOR, DOOR, FAMILY, LOCKPICK),
         "actor_id": ACTOR, "intent_family": FAMILY, "approach_signature": LOCKPICK,
         "outcome": "FAILURE", "retry_policy": "FORBIDDEN", "world_versions": {DOOR: 8}},
    ]
    found = find_prior_failure(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions={DOOR: 8},
    )
    assert found is not None and found["retry_policy"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# 7. 每种策略的判定
# ---------------------------------------------------------------------------
def test_free_retry_has_no_cost() -> None:
    ledger, versions = _ledger_with(policy=RetryPolicy.FREE)
    verdict = retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    )
    assert verdict.allowed is True
    assert verdict.requires_time is False
    assert verdict.escalates_risk is False


def test_costs_time_and_escalates_risk_allow_but_flag() -> None:
    for policy, flag in ((RetryPolicy.COSTS_TIME, "requires_time"),
                         (RetryPolicy.ESCALATES_RISK, "escalates_risk")):
        ledger, versions = _ledger_with(policy=policy)
        verdict = retry_verdict(
            ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
            approach_signature=LOCKPICK, world_versions=versions,
        )
        assert verdict.allowed is True
        assert getattr(verdict, flag) is True
        assert verdict.reason


def test_forbidden_and_changed_approach_both_block_but_explain_differently() -> None:
    reasons = {}
    for policy in (RetryPolicy.FORBIDDEN, RetryPolicy.REQUIRES_CHANGED_APPROACH):
        ledger, versions = _ledger_with(policy=policy)
        verdict = retry_verdict(
            ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
            approach_signature=LOCKPICK, world_versions=versions,
        )
        assert verdict.allowed is False
        reasons[policy] = verdict.reason
    assert reasons[RetryPolicy.FORBIDDEN] != reasons[RetryPolicy.REQUIRES_CHANGED_APPROACH], (
        "两者 gate 相同，但对叙事层要给不同理由"
    )


# ---------------------------------------------------------------------------
# 8. failure_matters：三问法的第三个入参
# ---------------------------------------------------------------------------
def test_first_attempt_is_conservative() -> None:
    """没有记录时不能替规则声明下结论。"""

    verdict = retry_verdict([], actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR)
    assert verdict.attempt is None
    assert failure_sticks(verdict) is True


def test_only_free_retry_makes_failure_meaningless() -> None:
    """FREE + 允许重试 → 失败不留痕 → 三问法该给 AUTO_SUCCESS。"""

    ledger, versions = _ledger_with(policy=RetryPolicy.FREE)
    verdict = retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    )
    assert failure_sticks(verdict) is False

    # 三问法接上去：失败无意义 → 直接成功，堵掉"我再试一次"
    assert three_question_resolution(
        can_succeed=True, can_fail=True, failure_matters=failure_sticks(verdict),
    ).name == "AUTO_SUCCESS"


@pytest.mark.parametrize("policy", [
    RetryPolicy.COSTS_TIME,
    RetryPolicy.ESCALATES_RISK,
    RetryPolicy.REQUIRES_CHANGED_CIRCUMSTANCE,
    RetryPolicy.REQUIRES_CHANGED_APPROACH,
    RetryPolicy.FORBIDDEN,
])
def test_other_policies_keep_failure_meaningful(policy: RetryPolicy) -> None:
    ledger, versions = _ledger_with(policy=policy)
    verdict = retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    )
    assert failure_sticks(verdict) is True


def test_verdict_serializes_for_the_narrator() -> None:
    ledger, versions = _ledger_with(policy=RetryPolicy.COSTS_TIME)
    payload = retry_verdict(
        ledger, actor_id=ACTOR, intent_family=FAMILY, target_id=DOOR,
        approach_signature=LOCKPICK, world_versions=versions,
    ).to_dict()
    assert payload["allowed"] is True
    assert payload["requires_time"] is True
    assert payload["attempt"]["consequence"] == ["lock_jammed"]
