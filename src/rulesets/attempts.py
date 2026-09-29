"""尝试记录（Attempt Ledger）：回答"这件事是不是已经试过了、还试不试得动"。

三问法里 ``can_succeed`` 与 ``can_fail`` 能从世界状态读出来，但
``failure_matters``（"失败有没有意义"）**只有尝试记录能回答**：

- 没有时间压力、可以无限重试 → 失败不留痕 → 这个骰子其实没有意义 → ``AUTO_SUCCESS``
- 试过了、锁卡住了、同样办法再来也没用 → 失败会留下 → 才值得掷骰

## key 是四元组，不是一个 action type

    AttemptKey(actor_id, target_id, intent_family, approach_signature)

因为 **goal 相同、approach 不同 → 完全是两回事**：

    撬锁失败        ≠  阻止  "我拿斧头砍门"
    pc_1 door_01 unlock lockpick
    pc_1 door_01 unlock force        ← 另一个 key，从未尝试过

把 ``approach_signature`` 放进 key 意味着"换手段"**自动**是另一次尝试，
不需要任何 ``if approach != last_approach`` 的特判 —— 换手段 = 换 key = 查不到记录。

## 失效靠版本，不靠手工 reset

记录里存的是**这次落完之后**的目标版本。GM 后来改了世界（把门重新锁上），
版本不匹配，旧记录**自动**不适用：

    attempt.world_versions == {"door_01": 8}
    GM 重新锁门 → door_01.version = 9
    applies_to({"door_01": 9}) → False → 允许重试

**记"尝试前"的版本是错的**：如果这次失败本身改动了目标（``lock_jammed``），
记前值会让记录**把自己作废**，于是可以无限重试。这是最容易写错的一处。

而且只比较**记录里出现过的目标**：别的对象变了不影响这条记录。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from src.rulesets.adjudication import OutcomeDegree, normalize_degree

__all__ = [
    "MAX_ATTEMPTS",
    "AttemptError",
    "RetryPolicy",
    "RetryVerdict",
    "attempt_applies",
    "attempt_key",
    "failure_sticks",
    "find_prior_failure",
    "load_attempts",
    "record_attempt",
    "retry_verdict",
    "world_versions",
]

#: 记录上限。超出后丢最旧的（诊断与失效判定只需要近期历史）。
MAX_ATTEMPTS = 128


class AttemptError(ValueError):
    """尝试记录不合法。``ValueError`` 子类，宿主服务层已按它处理。"""


class RetryPolicy(StrEnum):
    """这条记录对"**同样办法**再来一次"意味着什么。

    注意"换办法"不需要在这里表态：``approach_signature`` 是 key 的一部分，
    换办法就是另一个 key，本来就查不到记录。
    """

    #: 随便重试，没有代价。**失败不留痕** —— 该走 ``AUTO_SUCCESS``，别掷骰。
    FREE = "FREE"
    #: 可以重试，但每次都要付出时间。
    COSTS_TIME = "COSTS_TIME"
    #: 可以重试，但风险逐次升级（叙事层与风险轴要用它）。
    ESCALATES_RISK = "ESCALATES_RISK"
    #: 只有环境变了才能重试。靠 ``world_versions`` 自动失效，不需手工清。
    REQUIRES_CHANGED_CIRCUMSTANCE = "REQUIRES_CHANGED_CIRCUMSTANCE"
    #: 这个办法已经用尽了 —— 换办法可以，同样办法不行。
    REQUIRES_CHANGED_APPROACH = "REQUIRES_CHANGED_APPROACH"
    #: 同样办法永远不行（与上一条在 gate 上等价，区别只在给叙事层的理由）。
    FORBIDDEN = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class RetryVerdict:
    """"同样办法再来一次"的判定结果。"""

    allowed: bool
    #: 生效的策略；没有任何相关失败记录时是 ``""``。
    policy: str = ""
    requires_time: bool = False
    escalates_risk: bool = False
    reason: str = ""
    #: 生效的那条记录（如果有）。
    attempt: dict[str, Any] | None = None

    @property
    def has_history(self) -> bool:
        return self.attempt is not None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["attempt"] = dict(self.attempt) if self.attempt else None
        return data


# ---------------------------------------------------------------------------
# key
# ---------------------------------------------------------------------------
_KEY_SEP = "\x1f"  # 不可能出现在 id 里，避免 "a|b" 与 "a|b|c" 撞车


def attempt_key(
    actor_id: str, target_id: str = "", intent_family: str = "",
    approach_signature: str = "",
) -> str:
    """四元组 → 稳定字符串。空片段保留（``a||b`` 与 ``a|b|`` 不同，是有意的）。"""

    return _KEY_SEP.join(
        str(part or "").strip()
        for part in (actor_id, target_id, intent_family, approach_signature)
    )


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_attempts(raw: Any) -> list[dict[str, Any]]:
    """把 ``ruleset_state["attempts"]`` 读成列表（带校验）。"""

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AttemptError("attempts 必须是数组")
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise AttemptError(f"attempts[{index}] 必须是对象")
    return [dict(item) for item in raw]


def world_versions(raw_world: Mapping[str, Any], object_ids: Iterable[str] = ()) -> dict[str, int]:
    """取世界对象的当前版本快照。``object_ids`` 为空时取全部。

    只把**真正存在**的对象放进快照 —— 引用了不存在的对象会让记录永远不适用，
    那是静默失效，不如一开始就不记。
    """

    wanted = [str(x) for x in object_ids] or [str(k) for k in raw_world]
    snapshot: dict[str, int] = {}
    for object_id in wanted:
        raw = raw_world.get(object_id)
        if isinstance(raw, Mapping):
            snapshot[object_id] = int(raw.get("version") or 0)
    return snapshot


def record_attempt(
    ledger: list[dict[str, Any]],
    *,
    actor_id: str,
    outcome: OutcomeDegree | str,
    intent_family: str,
    target_id: str = "",
    approach_signature: str = "",
    retry_policy: RetryPolicy | str = RetryPolicy.FREE,
    world_versions: Mapping[str, int] | None = None,
    consequence: Iterable[str] = (),
    note: str = "",
) -> dict[str, Any]:
    """追加一条记录（原地修改 ``ledger``），返回写入的那条。

    调用方必须在**自己的效果落完之后**才取 ``world_versions``：记"尝试前"
    会让失败本身改目标的那些尝试把自己作废，从而变成可以无限重试。
    """

    actor = str(actor_id or "").strip()
    if not actor:
        raise AttemptError("尝试记录缺少 actor_id")
    family = str(intent_family or "").strip()
    if not family:
        raise AttemptError("尝试记录缺少 intent_family（用它区分「想干什么」）")
    degree = normalize_degree(outcome)
    if degree is None:
        raise AttemptError(f"无法识别的结果档位：{outcome!r}")
    try:
        policy = RetryPolicy(str(retry_policy or RetryPolicy.FREE))
    except ValueError as exc:
        raise AttemptError(f"未知的重试策略：{retry_policy!r}") from exc

    target = str(target_id or "").strip()
    versions = {str(k): int(v) for k, v in (world_versions or {}).items()}
    entry: dict[str, Any] = {
        "key": attempt_key(actor, target, family, approach_signature),
        "actor_id": actor,
        "target_id": target,
        "intent_family": family,
        "approach_signature": str(approach_signature or "").strip(),
        "outcome": str(degree),
        "retry_policy": str(policy),
        "world_versions": versions,
        "consequence": [str(x) for x in consequence if str(x).strip()],
        "note": str(note or "")[:120],
    }
    ledger.append(entry)
    if len(ledger) > MAX_ATTEMPTS:
        del ledger[: len(ledger) - MAX_ATTEMPTS]
    return entry


# ---------------------------------------------------------------------------
# 失效判定
# ---------------------------------------------------------------------------
def attempt_applies(attempt: Mapping[str, Any], current: Mapping[str, int]) -> bool:
    """记录是否仍适用于当前世界。

    只比较**记录里出现过**的目标：目标被删掉了算"变了"（不适用），
    别的对象变了则不影响。
    """

    recorded = attempt.get("world_versions")
    if not isinstance(recorded, Mapping):
        return True
    for object_id, version in recorded.items():
        if str(object_id) not in current:
            return False
        if int(current[str(object_id)] or 0) != int(version or 0):
            return False
    return True


def _is_failure(attempt: Mapping[str, Any]) -> bool:
    degree = normalize_degree(attempt.get("outcome"))
    return degree is not None and not degree.succeeded


def find_prior_failure(
    ledger: Iterable[Mapping[str, Any]],
    *,
    actor_id: str,
    intent_family: str,
    target_id: str = "",
    approach_signature: str = "",
    world_versions: Mapping[str, int] | None = None,
) -> dict[str, Any] | None:
    """找**最近一条仍适用的失败**记录。

    只看失败：成功之后不存在"还试不试得动"的问题。
    """

    key = attempt_key(actor_id, target_id, intent_family, approach_signature)
    current = {str(k): int(v) for k, v in (world_versions or {}).items()}
    for attempt in reversed(list(ledger)):
        if not isinstance(attempt, Mapping):
            continue
        if str(attempt.get("key") or "") != key:
            continue
        if not _is_failure(attempt):
            continue
        if attempt_applies(attempt, current):
            return dict(attempt)
    return None


def retry_verdict(
    ledger: Iterable[Mapping[str, Any]],
    *,
    actor_id: str,
    intent_family: str,
    target_id: str = "",
    approach_signature: str = "",
    world_versions: Mapping[str, int] | None = None,
) -> RetryVerdict:
    """同样办法再来一次，允不允许、要不要带代价。"""

    attempt = find_prior_failure(
        ledger, actor_id=actor_id, intent_family=intent_family,
        target_id=target_id, approach_signature=approach_signature,
        world_versions=world_versions,
    )
    if attempt is None:
        return RetryVerdict(
            allowed=True,
            reason="没有仍然适用的失败记录（首次尝试，或环境已变化）",
        )

    policy = str(attempt.get("retry_policy") or RetryPolicy.FREE)
    if policy == RetryPolicy.FREE:
        return RetryVerdict(
            allowed=True, policy=policy, attempt=attempt,
            reason="这条办法没有代价，可以随便重试 —— 失败不会留下痕迹",
        )
    if policy == RetryPolicy.COSTS_TIME:
        return RetryVerdict(
            allowed=True, policy=policy, requires_time=True, attempt=attempt,
            reason="可以重试，但每次都要付出时间",
        )
    if policy == RetryPolicy.ESCALATES_RISK:
        return RetryVerdict(
            allowed=True, policy=policy, escalates_risk=True, attempt=attempt,
            reason="可以重试，但风险逐次升级",
        )
    reasons = {
        RetryPolicy.REQUIRES_CHANGED_CIRCUMSTANCE: "要换环境才能再试（世界变了记录会自动失效）",
        RetryPolicy.REQUIRES_CHANGED_APPROACH: "这个办法已经用尽，换办法可以",
        RetryPolicy.FORBIDDEN: "同样办法不会再有结果",
    }
    return RetryVerdict(
        allowed=False, policy=policy, attempt=attempt,
        reason=reasons.get(policy, f"策略 {policy} 不允许重试"),
    )


def failure_sticks(verdict: RetryVerdict) -> bool:
    """这个失败会不会留下痕迹 —— 也就是三问法的 ``failure_matters``。

    只有"**没有相关失败记录**"与"**记录里是 FREE 且允许重试**"这两种情况
    才说明失败不留痕（可以无限重试 → 骰子无意义 → 该走 ``AUTO_SUCCESS``）。

    注意"没有记录"返回 ``True``（保守）：首次尝试时只有规则声明知道有没有
    时间压力，账本不该替它下结论。
    """

    if verdict.attempt is None:
        return True
    if not verdict.allowed:
        return True
    if verdict.requires_time or verdict.escalates_risk:
        return True
    return str(verdict.policy) != RetryPolicy.FREE
