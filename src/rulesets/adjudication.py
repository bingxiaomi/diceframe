"""裁定契约（Adjudication Contract）：权威意图这一层的共享词汇。

这一层解决的问题是"**谁赢了由谁说了算，以及怎么说给别人听**"。

设计来源（借结构，不抄代码）：

- **D&D 2024 基础规则**借"判断程序"：只有"可能成功 + 可能失败 + 失败有代价"
  三者皆真才掷骰；以及 Influence 的 Willing / Unwilling / Hesitant 结构。
- **Foundry VTT dnd5e** 借"程序化表示"：``Activity``（可执行单元）与
  ``ActiveEffect``（基础值 + 修正量 = 有效值）。change 类型词汇
  ``add / subtract / multiply / override / upgrade / downgrade`` 与效果的
  ``origin`` 溯源直接沿用。
- **Pathfinder 2e** 借"结果分级"：``critical success / success / failure /
  critical failure``。额外加一档 ``PARTIAL_SUCCESS``（"撬开了但惊动了里面的人"）
  给叙事层用。
- **Blades in the Dark** 借"自由意图的裁定"：先定 **Position（风险）** 与
  **Effect（效果）**，再决定怎么掷。**这两项是运行时内部语义层**，不是要求
  规则集放弃自己的 DC 体系 —— 它们最终映射到 DC / 劣势 / 后果严重度。

三条硬约束（本模块只负责**校验**，执行在各 runtime）：

1. ``Resolution`` 是宿主唯一需要懂的控制流词汇，**必须保持小**。
   规则集自己那些细腻档位（极难成功 / 大成功 / raise / complication）
   属于 ``OutcomeDegree`` 与事件 payload，不属于控制流。
2. **客户端不能提供骰子结果**。``Activity`` 里没有任何"掷出多少"的字段；
   骰子只能由服务端 RNG 产生。``intent_field_violations`` 用于拒绝越界字段。
3. 裁定必须**可复现、可回放**：``to_event`` / ``from_event`` 是同一份数据的
   两种视图，往返不掉信息。

这一层刻意**不做**策略计算（那属于 runtime 的 ``resolve_intent``），
只做词汇、校验与转换。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping, Sequence

__all__ = [
    "ACTIVITY_TYPES",
    "CONSUMPTION_FAILED",
    "ConsumptionTiming",
    "EFFECT_CHANGE_OPS",
    "EFFECT_KINDS",
    "Adjudication",
    "AdjudicationError",
    "Activity",
    "Competence",
    "CompetenceLevel",
    "Effect",
    "EffectDescriptor",
    "OutcomeDegree",
    "Resolution",
    "Risk",
    "Stakes",
    "check_resolved_event",
    "compute_change",
    "ConsequenceSeverity",
    "CostLine",
    "GoalOutcome",
    "OutcomeBreakdown",
    "RuleTrace",
    "SeverityScale",
    "TraceEntry",
    "effect_descriptors_from",
    "intent_field_violations",
    "normalize_degree",
    "severity_for_degree",
    "steps_below_success",
    "three_question_resolution",
]


class AdjudicationError(ValueError):
    """裁定数据不合法。``ValueError`` 子类：宿主服务层已按 ValueError 处理。"""


# ---------------------------------------------------------------------------
# 1. 控制流：宿主需要知道的最小词汇
# ---------------------------------------------------------------------------
class Resolution(StrEnum):
    """宿主靠这个决定控制流：要不要掷骰、要不要叙述、要不要回问玩家。

    **刻意只有 5 个值。** 多加一个档位前先问：宿主会因此走不同的分支吗？
    如果不会（例如"极难成功"和"成功"），它就该是 :class:`OutcomeDegree`
    或事件 payload 里的事，不是控制流。
    """

    #: 不需要掷骰，直接成功。门的锁普通、角色专业、时间无限。
    AUTO_SUCCESS = "AUTO_SUCCESS"
    #: 不需要掷骰，直接失败。徒手推倒城墙；说服国王送出国土。
    AUTO_FAILURE = "AUTO_FAILURE"
    #: 连尝试本身都不成立（世界状态不允许 / 手段根本不可能）。
    #: 与 AUTO_FAILURE 的区别在叙述：这里是"做不到"，那里是"做了但没成"。
    IMPOSSIBLE = "IMPOSSIBLE"
    #: 存在不确定性且有意义的失败 —— 只有这时才掷骰。
    CHECK_REQUIRED = "CHECK_REQUIRED"
    #: 信息不足，需要玩家补充目标或手段。**应当少用**：
    #: 频繁回问会把自由跑团变成填表。
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"

    @property
    def requires_check(self) -> bool:
        """宿主是否需要走掷骰路径。"""

        return self is Resolution.CHECK_REQUIRED

    @property
    def needs_clarification(self) -> bool:
        """宿主是否应该回问玩家而不是继续叙事。"""

        return self is Resolution.NEEDS_CLARIFICATION

    @property
    def outcome_sign(self) -> int:
        """+1 正面 / 0 未知 / -1 负面。宿主用它决定叙述基调。"""

        if self in (Resolution.AUTO_SUCCESS,):
            return 1
        if self in (Resolution.AUTO_FAILURE, Resolution.IMPOSSIBLE):
            return -1
        return 0


# ---------------------------------------------------------------------------
# 2. 结果分级：规则集与叙事层用的细腻档位
# ---------------------------------------------------------------------------
class OutcomeDegree(StrEnum):
    """一次裁定的结果档位（PF2e 的四档 + 叙事用的 PARTIAL_SUCCESS）。

    规则集**不必**用满：D&D 只用 SUCCESS / FAILURE，我们的 d100 用
    SUCCESS / FAILURE 加上自己的极难/困难档。用 :func:`normalize_degree`
    把自己那套标签映射过来即可。
    """

    CRITICAL_SUCCESS = "CRITICAL_SUCCESS"
    SUCCESS = "SUCCESS"
    #: "你成功撬开门，但响声惊动了里面的人。" —— AI 跑团最有用的一档。
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILURE = "FAILURE"
    CRITICAL_FAILURE = "CRITICAL_FAILURE"

    @property
    def succeeded(self) -> bool:
        return self in (
            OutcomeDegree.CRITICAL_SUCCESS,
            OutcomeDegree.SUCCESS,
            OutcomeDegree.PARTIAL_SUCCESS,
        )

    @property
    def severity(self) -> int:
        """+2 / +1 / +1 / -1 / -2。用于比较"哪次更好"。"""

        return {
            OutcomeDegree.CRITICAL_SUCCESS: 2,
            OutcomeDegree.SUCCESS: 1,
            OutcomeDegree.PARTIAL_SUCCESS: 1,
            OutcomeDegree.FAILURE: -1,
            OutcomeDegree.CRITICAL_FAILURE: -2,
        }[self]


#: 宽松映射表：runtime 自己的档位标签 → 契约档位。
#: 只覆盖常见叫法，命中不了就交给规则集自己声明，不做猜测。
_DEGREE_ALIASES: dict[str, OutcomeDegree] = {
    "critical_success": OutcomeDegree.CRITICAL_SUCCESS,
    "crit": OutcomeDegree.CRITICAL_SUCCESS,
    "extreme": OutcomeDegree.CRITICAL_SUCCESS,
    "critical": OutcomeDegree.CRITICAL_SUCCESS,
    "success": OutcomeDegree.SUCCESS,
    "hard": OutcomeDegree.SUCCESS,
    "regular": OutcomeDegree.SUCCESS,
    "pass": OutcomeDegree.SUCCESS,
    "partial": OutcomeDegree.PARTIAL_SUCCESS,
    "partial_success": OutcomeDegree.PARTIAL_SUCCESS,
    "mixed": OutcomeDegree.PARTIAL_SUCCESS,
    "success_with_cost": OutcomeDegree.PARTIAL_SUCCESS,
    "failure": OutcomeDegree.FAILURE,
    "fail": OutcomeDegree.FAILURE,
    "fumble": OutcomeDegree.CRITICAL_FAILURE,
    "critical_failure": OutcomeDegree.CRITICAL_FAILURE,
}


def normalize_degree(value: Any) -> OutcomeDegree | None:
    """把任意 runtime 的档位标签映射成契约档位；认不出返回 ``None``。

    刻意返回 ``None`` 而不是猜一个 —— 猜错会把失败写成成功。
    """

    if isinstance(value, OutcomeDegree):
        return value
    text = str(value or "").strip().lower().replace("-", "").replace(" ", "_")
    text = text.replace("_", "_")
    if not text:
        return None
    direct = _DEGREE_ALIASES.get(text)
    if direct is not None:
        return direct
    # 再来一轮：去掉下划线后比对，覆盖 extremeSuccess / PartialSuccess 之类。
    squashed = text.replace("_", "")
    for alias, degree in _DEGREE_ALIASES.items():
        if alias.replace("_", "") == squashed:
            return degree
    return None


# ---------------------------------------------------------------------------
# 3. 风险 / 效果：Blades in the Dark 的两条语义轴
# ---------------------------------------------------------------------------
class Risk(StrEnum):
    """这次行动有多危险（**环境决定，不是角色数值决定**）。

    "冲过去一个人打五个守卫"是 EXTREME；"退进只能过一人的窄巷"同样的角色
    只是 HIGH 或 STANDARD。环境改变了风险，而不是角色改变了数值。
    """

    LOW = "LOW"
    STANDARD = "STANDARD"
    HIGH = "HIGH"
    EXTREME = "EXTREME"

    @property
    def dc_modifier(self) -> int:
        """映射到 DC 的偏移量。想让"为什么这个做法更好"体现在数字上。"""

        return {
            Risk.LOW: -4,
            Risk.STANDARD: 0,
            Risk.HIGH: 4,
            Risk.EXTREME: 8,
        }[self]

    @property
    def grants_disadvantage(self) -> bool:
        """高风险默认给劣势（规则集可覆盖）。"""

        return self in (Risk.HIGH, Risk.EXTREME)


class Effect(StrEnum):
    """这次行动最好能做到什么程度。"""

    NONE = "NONE"
    LIMITED = "LIMITED"
    STANDARD = "STANDARD"
    GREAT = "GREAT"

    @property
    def degree_shift(self) -> int:
        """效果档位对结果分级的偏移。

        ``LIMITED`` 把 CRITICAL_SUCCESS 压成 SUCCESS（"做到了，但也就那样"）；
        ``GREAT`` 把 SUCCESS 抬成 CRITICAL_SUCCESS。
        """

        return {
            Effect.NONE: -2,
            Effect.LIMITED: -1,
            Effect.STANDARD: 0,
            Effect.GREAT: 1,
        }[self]


# ---------------------------------------------------------------------------
# 4. 效果描述符：ActiveEffect 兼容的最小形状
# ---------------------------------------------------------------------------
#: 沿用 Foundry dnd5e 的 change 类型词汇（借结构，不抄实现）。
EFFECT_CHANGE_OPS = (
    "add", "subtract", "multiply", "override", "upgrade", "downgrade",
)

#: 效果作用的对象类别。刻意保持粗粒度：宿主只靠它决定去哪儿找目标。
EFFECT_KINDS = ("resource", "attribute", "condition", "item", "world", "score")


@dataclass(frozen=True, slots=True)
class EffectDescriptor:
    """一条"世界状态要变成什么样"的描述。

    形状对齐 Founder 的 ActiveEffect change（``key`` / ``type`` / ``value``），
    这样 :attr:`stakes` 里写的东西可以直接被落成效果，而不是直接改基础数值。
    **不要直接改 ``attributes``** —— 一旦把 ``bless`` 写进 ``dex``，以后就再也
    分不清哪部分是角色底子、哪部分是一次性加值。

    ``source`` 是溯源（对应 Foundry 的 ``origin``）：哪次裁定产生的。
    没有它就无法回答"这个 +1d4 是哪来的、什么时候到期"。
    """

    kind: str
    target: str
    field: str
    op: str
    value: Any
    #: 持续时间。``None`` = 永久（直到被显式移除）。形状对齐
    #: ``{"type": "round"|"turn"|"rest"|"scene", "remaining": int}``。
    duration: dict[str, Any] | None = None
    source: str = ""

    def validate(self) -> None:
        if self.kind not in EFFECT_KINDS:
            raise AdjudicationError(
                f"未知的效果类别 {self.kind!r}（合法值：{', '.join(EFFECT_KINDS)}）"
            )
        if self.op not in EFFECT_CHANGE_OPS:
            raise AdjudicationError(
                f"未知的效果算子 {self.op!r}（合法值：{', '.join(EFFECT_CHANGE_OPS)}）"
            )
        if not str(self.target or "").strip():
            raise AdjudicationError("效果缺少 target")
        if not str(self.field or "").strip():
            raise AdjudicationError("效果缺少 field")
        if self.duration is not None:
            if not isinstance(self.duration, dict):
                raise AdjudicationError("效果 duration 必须是对象或 null")
            kind = str(self.duration.get("type") or "")
            if kind not in ("", "round", "turn", "rest", "scene"):
                raise AdjudicationError(f"未知的持续时间类型 {kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Stakes:
    """失败的代价与成功的收获。

    **这是叙事层被约束的地方，也是它仍然自由的边界**：

    - 权威层决定 **发生了什么**（事件与效果）；
    - ``stakes`` 告诉叙事层 **必须提到什么**（失败的代价不能漏写）；
    - 叙事层仍然自由决定 **怎么讲**（文风、细节、节奏）。

    约束事实，不约束文笔。约束了文笔就变模板文了。
    """

    success: tuple[EffectDescriptor, ...] = ()
    partial: tuple[EffectDescriptor, ...] = ()
    failure: tuple[EffectDescriptor, ...] = ()

    def validate(self) -> None:
        for group, items in (
            ("success", self.success), ("partial", self.partial), ("failure", self.failure),
        ):
            for item in items:
                if not isinstance(item, EffectDescriptor):
                    raise AdjudicationError(f"stakes.{group} 里混入了非效果描述符")
                item.validate()

    def for_degree(self, degree: OutcomeDegree | None) -> tuple[EffectDescriptor, ...]:
        """按结果档位取应该落地的效果。"""

        if degree is None:
            return ()
        if degree.succeeded:
            return self.success if degree is not OutcomeDegree.PARTIAL_SUCCESS else self.partial
        return self.failure

    def describe(self) -> list[str]:
        """给叙事层和玩家看的人话摘要。"""

        lines: list[str] = []
        for label, items in (
            ("成功", self.success), ("部分成功", self.partial), ("失败", self.failure),
        ):
            for item in items:
                lines.append(f"{label}：{item.target}.{item.field} {item.op} {item.value}")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": [item.to_dict() for item in self.success],
            "partial": [item.to_dict() for item in self.partial],
            "failure": [item.to_dict() for item in self.failure],
        }


# ---------------------------------------------------------------------------
# 5. 胜任度：专业角色不该因为一次低骰变得像白痴
# ---------------------------------------------------------------------------
class CompetenceLevel(StrEnum):
    """从角色卡**派生**出来的胜任度，不是新增的规则等级。

    ``PROFICIENT`` 及以上配"时间无限 + 失败无代价 + 目标普通"，就该直接
    ``AUTO_SUCCESS``：20 年经验的船长不该因为掷出 1 就把船撞沉。
    """

    UNTRAINED = "UNTRAINED"
    FAMILIAR = "FAMILIAR"
    PROFICIENT = "PROFICIENT"
    EXPERT = "EXPERT"
    MASTER = "MASTER"

    @property
    def at_least_proficient(self) -> bool:
        return self in (
            CompetenceLevel.PROFICIENT,
            CompetenceLevel.EXPERT,
            CompetenceLevel.MASTER,
        )


@dataclass(frozen=True, slots=True)
class Competence:
    """本次行动相关的胜任度评估。``evidence`` 是可追溯的来源说明。"""

    level: CompetenceLevel = CompetenceLevel.UNTRAINED
    ability_modifier: int = 0
    training: tuple[str, ...] = ()
    tool: str = ""
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["level"] = str(self.level)
        return data


# ---------------------------------------------------------------------------
# 6. Activity：可执行单元（借 Foundry 的 Activity 概念）
# ---------------------------------------------------------------------------
#: 可执行动作类型。宿主**不需要**懂全部 —— 它只负责投影给前端；
#: 真正认识每个类型、并产出对应事件的是规则集自己的 reducer。
ACTIVITY_TYPES = (
    "check", "attack", "save", "damage", "heal",
    "cast", "use", "summon", "teleport", "transform", "utility",
)

#: 消费失败时的错误码（对应 Foundry 的 ``ConsumptionError``）：
#: 资源不够 **不是静默跳过，而是让整次裁定失败**。
CONSUMPTION_FAILED = "CONSUMPTION_FAILED"


class ConsumptionTiming(StrEnum):
    """代价在哪个点上被扣。三者的差别玩家能直接感知到。

    - ``ON_DECLARE``：**说了就算**。打断、放弃、甚至规则判定"这不可能成"，
      代价都已经付了。典型是法术位 —— 手势起手就烧掉了。
    - ``ON_RESOLVE``：**掷了就付**。成或败都付，但没掷就不付。
    - ``ON_SUCCESS``：**成了才付**。失败时资源留着 —— "失败不消耗"本身就是
      规则表述的一部分（例如只在搜到东西时才计入体力消耗）。
    """

    ON_DECLARE = "ON_DECLARE"
    ON_RESOLVE = "ON_RESOLVE"
    ON_SUCCESS = "ON_SUCCESS"


def consumption_charges(timing: Any, *, rolled: bool, succeeded: bool) -> bool:
    """这次裁定里，某个时机该不该扣。

    ``rolled=False`` 表示检定没掷骰（被骰前裁定短路了）。于是三种时机
    自然分成三行：ON_DECLARE 不看结果，ON_RESOLVE 看有没有掷，ON_SUCCESS
    两个都要。
    """

    if timing == ConsumptionTiming.ON_DECLARE:
        return True
    if timing == ConsumptionTiming.ON_RESOLVE:
        return rolled
    return rolled and succeeded


def consumption_checked_before_roll(timing: Any) -> bool:
    """这个时机是否需要在**掷骰前**就检查付得起。

    ``ON_SUCCESS`` 返回 False —— "失败不消耗"就是它的全部意义，
    骰前拦截等于偷偷把它变成 ``ON_RESOLVE``。
    """

    return timing in (ConsumptionTiming.ON_DECLARE, ConsumptionTiming.ON_RESOLVE)


@dataclass(frozen=True, slots=True)
class Activity:
    """被裁定出来的、可以被执行的规则单元。

    ``Attack / Check / Save / Damage / Heal / Cast / Summon / Teleport /
    Transform / Use`` 这一组类型名来自 Foundry dnd5e；它们的价值是让"玩家想做什么"
    和"要执行什么规则"分开：同一个 Goal 可以走完全不同的 Activity。

    **这里没有骰子结果字段** —— 骰子由注入的服务端 RNG 产生，客户端不许提供。
    """

    type: str
    actor_id: str
    ability: str = ""
    skill: str = ""
    dc: int | None = None
    target: str = ""
    #: 执行这次动作要付出什么（对齐 Foundry 的 ``consumption.targets``）。
    consumption: tuple[EffectDescriptor, ...] = ()
    #: 结果持续时间（对齐 Foundry 的 ``Activity.duration``）。
    duration: dict[str, Any] | None = None
    #: 溯源：这次动作由哪个意图/规则条目产生。
    source: str = ""

    def validate(self) -> None:
        if self.type not in ACTIVITY_TYPES:
            raise AdjudicationError(
                f"未知的动作类型 {self.type!r}（合法值：{', '.join(ACTIVITY_TYPES)}）"
            )
        if not str(self.actor_id or "").strip():
            raise AdjudicationError("动作缺少 actor_id")
        if self.dc is not None:
            try:
                if int(self.dc) < 0:
                    raise AdjudicationError("DC 不能为负")
            except (TypeError, ValueError) as exc:
                raise AdjudicationError(f"DC 必须是整数：{self.dc!r}") from exc
        for item in self.consumption:
            if not isinstance(item, EffectDescriptor):
                raise AdjudicationError("consumption 里混入了非效果描述符")
            item.validate()

    @property
    def needs_roll(self) -> bool:
        return self.type in ("check", "attack", "save")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "actor_id": self.actor_id,
            "ability": self.ability,
            "skill": self.skill,
            "dc": self.dc,
            "target": self.target,
            "consumption": [item.to_dict() for item in self.consumption],
            "duration": self.duration,
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# 7. 一次裁定的完整记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Adjudication:
    """一次权威裁定的完整结果。

    它就是 ``check.resolved`` 事件的 payload —— 用既有事件类型承载，
    不新造外壳（``check.resolved`` 在 D&D 的 reducer 里本来就是 no-op）。
    """

    resolution: Resolution
    #: 玩家想要什么。
    goal: str = ""
    #: 玩家打算怎么做。**Goal 相同、Approach 不同 → 规则路径完全不同。**
    approach: str = ""
    activity: Activity | None = None
    risk: Risk = Risk.STANDARD
    effect_level: Effect = Effect.STANDARD
    competence: Competence | None = None
    stakes: Stakes | None = None
    #: 为什么这么判。给人看的人话，会被叙事层引用。
    reason: tuple[str, ...] = ()
    #: 规则集自己的档位标签（极难成功 / fumble / raise…）。宿主不解释它。
    degree: OutcomeDegree | None = None
    #: 服务端掷骰结果的摘要（**只能由服务端填**）。
    roll: dict[str, Any] | None = None

    # -- 派生的宿主信号 ----------------------------------------------------
    @property
    def requires_check(self) -> bool:
        return self.resolution.requires_check

    @property
    def needs_clarification(self) -> bool:
        return self.resolution.needs_clarification

    @property
    def outcome_sign(self) -> int:
        return self.resolution.outcome_sign

    # -- 校验 --------------------------------------------------------------
    def validate(self) -> None:
        if self.resolution.requires_check and self.activity is None:
            raise AdjudicationError("CHECK_REQUIRED 必须给出 activity（否则宿主不知道掷什么）")
        if self.resolution.requires_check and self.activity is not None:
            if self.activity.dc is None and not self.activity.skill and not self.activity.ability:
                raise AdjudicationError(
                    "CHECK_REQUIRED 的 activity 至少要给出 dc / skill / ability 之一"
                )
        if not self.resolution.requires_check and self.activity is not None:
            if self.activity.needs_roll:
                raise AdjudicationError(
                    f"{self.resolution} 不该带需要掷骰的 activity（{self.activity.type}）"
                )
        if self.activity is not None:
            self.activity.validate()
        if self.stakes is not None:
            self.stakes.validate()
        if self.roll is not None and not isinstance(self.roll, dict):
            raise AdjudicationError("roll 必须是对象或 null")
        if not self.resolution.needs_clarification and not self.reason:
            raise AdjudicationError("裁定必须给出至少一条 reason（人话），否则叙事层无从解释")

    # -- 事件互转 ----------------------------------------------------------
    def to_event(self, *, source: str = "") -> dict[str, Any]:
        """转成 ``check.resolved`` 事件 payload（可直接放进 EventBatch）。"""

        self.validate()
        payload: dict[str, Any] = {
            "type": "check.resolved",
            "resolution": str(self.resolution),
            "goal": self.goal,
            "approach": self.approach,
            "risk": str(self.risk),
            "effect": str(self.effect_level),
            "reason": list(self.reason),
            "source": source,
        }
        if self.activity is not None:
            payload["activity"] = self.activity.to_dict()
        if self.competence is not None:
            payload["competence"] = self.competence.to_dict()
        if self.stakes is not None:
            payload["stakes"] = self.stakes.to_dict()
        if self.degree is not None:
            payload["degree"] = str(self.degree)
        if self.roll is not None:
            payload["roll"] = dict(self.roll)
        return payload

    @classmethod
    def from_event(cls, payload: Mapping[str, Any]) -> "Adjudication":
        """从事件 payload 还原。与 :meth:`to_event` 往返不掉信息。"""

        if not isinstance(payload, Mapping):
            raise AdjudicationError("裁定 payload 必须是对象")
        try:
            resolution = Resolution(str(payload.get("resolution") or ""))
        except ValueError as exc:
            raise AdjudicationError(f"未知的 resolution：{payload.get('resolution')!r}") from exc

        raw_activity = payload.get("activity")
        activity = None
        if isinstance(raw_activity, Mapping):
            activity = Activity(
                type=str(raw_activity.get("type") or ""),
                actor_id=str(raw_activity.get("actor_id") or ""),
                ability=str(raw_activity.get("ability") or ""),
                skill=str(raw_activity.get("skill") or ""),
                dc=raw_activity.get("dc"),
                target=str(raw_activity.get("target") or ""),
                consumption=_descriptors_from(raw_activity.get("consumption")),
                duration=raw_activity.get("duration"),
                source=str(raw_activity.get("source") or ""),
            )

        raw_stakes = payload.get("stakes")
        stakes = None
        if isinstance(raw_stakes, Mapping):
            stakes = Stakes(
                success=_descriptors_from(raw_stakes.get("success")),
                partial=_descriptors_from(raw_stakes.get("partial")),
                failure=_descriptors_from(raw_stakes.get("failure")),
            )

        raw_competence = payload.get("competence")
        competence = None
        if isinstance(raw_competence, Mapping):
            competence = Competence(
                level=_competence_level(raw_competence.get("level")),
                ability_modifier=int(raw_competence.get("ability_modifier") or 0),
                training=tuple(str(x) for x in (raw_competence.get("training") or ())),
                tool=str(raw_competence.get("tool") or ""),
                evidence=tuple(str(x) for x in (raw_competence.get("evidence") or ())),
            )

        return cls(
            resolution=resolution,
            goal=str(payload.get("goal") or ""),
            approach=str(payload.get("approach") or ""),
            activity=activity,
            risk=_risk(payload.get("risk")),
            effect_level=_effect(payload.get("effect")),
            competence=competence,
            stakes=stakes,
            reason=tuple(str(x) for x in (payload.get("reason") or ())),
            degree=normalize_degree(payload.get("degree")),
            roll=dict(payload["roll"]) if isinstance(payload.get("roll"), Mapping) else None,
        )

    # -- 便捷构造 ----------------------------------------------------------
    def with_degree(self, degree: OutcomeDegree | str) -> "Adjudication":
        """追加结果档位（掷骰之后）。"""

        parsed = normalize_degree(degree)
        if parsed is None:
            raise AdjudicationError(f"无法识别的结果档位：{degree!r}")
        return replace(self, degree=parsed)

    def with_roll(self, roll: Mapping[str, Any]) -> "Adjudication":
        """追加服务端掷骰摘要。**只应由服务端调用。**"""

        return replace(self, roll=dict(roll))


# ---------------------------------------------------------------------------
# 8. 默认的三问法（纯函数，规则集可覆盖）
# ---------------------------------------------------------------------------
def three_question_resolution(
    *, can_succeed: bool, can_fail: bool, failure_matters: bool,
) -> Resolution:
    """D&D 2024 基础规则的判断程序，落成纯函数。

    只有 **三个都为真** 才掷骰：

    ================  ==========  ==========  ==============  ==================
    can_succeed       can_fail    fail_matter  结果            例子
    ================  ==========  ==========  ==============  ==================
    True              False       —           AUTO_SUCCESS    打开一扇没锁的门
    False             —           —           IMPOSSIBLE      徒手推倒城墙
    True              True        False       AUTO_SUCCESS    无限时间反复开锁
    True              True        True        CHECK_REQUIRED  火灾里 20 秒撬锁
    ================  ==========  ==========  ==============  ==================

    "失败有意义"是关键：如果玩家可以无限重试，总有一次成功，那这个骰子
    实际上没有意义 —— 应该给 ``AUTO_SUCCESS``，而不是允许"我再检查一次"刷骰。
    """

    if not can_succeed:
        return Resolution.IMPOSSIBLE
    if not can_fail:
        return Resolution.AUTO_SUCCESS
    if not failure_matters:
        return Resolution.AUTO_SUCCESS
    return Resolution.CHECK_REQUIRED


# ---------------------------------------------------------------------------
# 9. 硬约束：客户端不许提供骰子结果
# ---------------------------------------------------------------------------
#: 意图里**禁止**出现的键：这些是"**掷出了什么**"，不是"用什么掷"。
#:
#: 客户端只能声明"我想做什么"，不能声明"我掷出了什么"。骰子由注入的服务端
#: RNG 产生 —— 这是"Natural 20 不能改写现实"从**实现纪律**变成**协议强制**的
#: 地方：runtime 就算想读 ``intent["d20"]``，也应该先被这里挡掉。
#:
#: 刻意**不含** ``dice`` / ``modifier`` / ``advantage`` / ``disadvantage``：
#: 那些是描述符（用什么骰子、有没有加值），不是结果；而且可能出现在
#: ``available_intents`` 的返回值里被前端回传。把它们一并禁掉只会误伤合法流程。
FORBIDDEN_INTENT_KEYS = frozenset({
    "d20", "roll", "rolls", "total", "result", "outcome", "degree",
    "success", "succeeded", "critical", "nat20", "natural_20", "server_roll",
})


def intent_field_violations(intent: Mapping[str, Any]) -> list[str]:
    """返回意图里越界的字段名（递归检查嵌套对象）。

    这是"客户端不能伪造骰子"从**实现纪律**变成**协议强制**的地方：
    一个 runtime 就算想读 ``intent["d20"]``，也应该先被这里挡掉。
    """

    violations: list[str] = []
    stack: list[tuple[str, Any]] = [("", intent)]
    while stack:
        prefix, node = stack.pop()
        if isinstance(node, Mapping):
            for key, value in node.items():
                name = str(key)
                path = f"{prefix}.{name}" if prefix else name
                if name.strip().lower() in FORBIDDEN_INTENT_KEYS:
                    violations.append(path)
                    continue
                stack.append((path, value))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                stack.append((f"{prefix}[{index}]", value))
    return sorted(violations)


# ---------------------------------------------------------------------------
# 10. 小工具
# ---------------------------------------------------------------------------
def check_resolved_event(adjudication: Adjudication, *, source: str = "") -> dict[str, Any]:
    """便捷入口：裁定 → EventBatch 里的事件。"""

    return adjudication.to_event(source=source)


def effect_descriptors_from(value: Any) -> tuple[EffectDescriptor, ...]:
    """把事件 payload 里的效果数组还原成描述符。

    ``world.changed`` 这类事件携带的是 JSON，落到状态前要过一遍校验。
    不合法就抛 :class:`AdjudicationError` —— 不猜、也不静默丢弃。
    """

    return _descriptors_from(value)


def compute_change(
    current: Any, op: str, value: Any, *, where: str = "",
) -> tuple[Any, bool]:
    """算子语义的**唯一实现**：算新值，返回 ``(新值, 是否真的变了)``。

    世界状态（:mod:`src.rulesets.world`）与角色活跃效果
    （:mod:`src.rulesets.effects`）共用这一份 —— 两处各写一遍必然会漂移，
    而且漂移的方式很隐蔽（同一个 ``upgrade`` 在两处取了不同的方向）。

    不认识 / 不该出现的组合直接报错，不猜：

    - ``override`` 接受任何值（包括布尔与字符串）；
    - 其余五个算子在**布尔**字段上直接报错（布尔没有加法）；
    - 其余五个算子要求两侧都是数值；
    - 字符串公式（``"1d6"``）碰到非字符串当前值时报错 —— 公式必须先在
      ``resolve_intent`` 里用服务端 RNG 解析成具体数值。
    """

    spot = where or "<value>"
    if isinstance(value, str) and not isinstance(current, str):
        raise AdjudicationError(
            f"{spot} 的 value 是字符串 {value!r} 而当前值不是 —— "
            "公式必须在 resolve_intent 里用服务端 RNG 解析成具体数值再提交"
        )
    if op not in EFFECT_CHANGE_OPS:
        raise AdjudicationError(
            f"{spot}：未知算子 {op!r}（合法值：{', '.join(EFFECT_CHANGE_OPS)}）"
        )
    if op == "override":
        return deepcopy(value), value != current
    if isinstance(current, bool) or isinstance(value, bool):
        raise AdjudicationError(f"{spot}：布尔字段只支持 override，收到 {op!r}")
    if not isinstance(current, (int, float)) or not isinstance(value, (int, float)):
        raise AdjudicationError(
            f"{spot}：{op!r} 需要两侧都是数值，收到 "
            f"{type(current).__name__} 与 {type(value).__name__}"
        )
    if op == "add":
        return current + value, True
    if op == "subtract":
        return current - value, True
    if op == "multiply":
        return current * value, True
    if op == "upgrade":
        # Foundry 语义：取更高者 —— D&D 的"同类加值不叠加"靠它。
        return (value, True) if value > current else (current, False)
    # downgrade：取更低者。
    return (value, True) if value < current else (current, False)


def _descriptors_from(value: Any) -> tuple[EffectDescriptor, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[EffectDescriptor] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise AdjudicationError("效果列表里混入了非对象元素")
        result.append(EffectDescriptor(
            kind=str(item.get("kind") or ""),
            target=str(item.get("target") or ""),
            field=str(item.get("field") or ""),
            op=str(item.get("op") or ""),
            value=item.get("value"),
            duration=item.get("duration") if isinstance(item.get("duration"), Mapping) else None,
            source=str(item.get("source") or ""),
        ))
    return tuple(result)


def _risk(value: Any) -> Risk:
    try:
        return Risk(str(value or "").strip().upper())
    except ValueError:
        return Risk.STANDARD


def _effect(value: Any) -> Effect:
    try:
        return Effect(str(value or "").strip().upper())
    except ValueError:
        return Effect.STANDARD


def _competence_level(value: Any) -> CompetenceLevel:
    try:
        return CompetenceLevel(str(value or "").strip().upper())
    except ValueError:
        return CompetenceLevel.UNTRAINED


# ---------------------------------------------------------------------------
# 7. 结果分解：把"部分成功"从一个魔法枚举拆成两个问题
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GoalOutcome:
    """目标达成情况：**成没成**（``achieved``）与**成了多少**（``extent``）。

    这两件事本来就是分开的，而"部分成功"这种档位名把它们压成了一个枚举值 ——
    于是每一本规则书都要自己发明一套名字（raise a consequence / success with
    cost / mixed result），而宿主并不知道那些名字之间是什么关系。
    """

    achieved: bool
    extent: float = 1.0

    @property
    def partial(self) -> bool:
        """达成了一部分 —— 这才是 ``PARTIAL_SUCCESS`` 真正想说的东西。"""

        return self.achieved and 0.0 < self.extent < 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "achieved": self.achieved,
            "extent": self.extent,
            "partial": self.partial,
        }


@dataclass(frozen=True, slots=True)
class CostLine:
    """一笔代价的**事实**（不是声明）。声明在 ``custom_mechanics.consumptions``。"""

    resource: str
    amount: int
    timing: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "amount": self.amount,
            "timing": self.timing,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OutcomeBreakdown:
    """一次裁定的完整结果：档位 + 目标达成 + 严重度 + 代价。

    ``degree`` 只作为**规则书自己的名字**保留（用于显示与翻译），判断不该读它
    —— 该读的是 ``goal`` 与 ``severity``。
    """

    degree: str
    degree_label: str
    resolution: str
    goal: GoalOutcome
    severity: str
    costs: tuple[CostLine, ...] = ()

    @classmethod
    def from_degree(
        cls, *, degree_id: str, degree_label: str, resolution: Any,
        achieved: bool, extent: float, severity: Any = None,
    ) -> OutcomeBreakdown:
        return cls(
            degree=degree_id,
            degree_label=degree_label,
            resolution=str(resolution),
            goal=GoalOutcome(achieved=achieved, extent=extent),
            severity=str(severity or ConsequenceSeverity.NONE),
        )

    def with_costs(self, costs: Sequence[CostLine]) -> OutcomeBreakdown:
        return replace(self, costs=tuple(costs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "degree": self.degree,
            "degree_label": self.degree_label,
            "resolution": self.resolution,
            "goal": self.goal.to_dict(),
            "severity": self.severity,
            "costs": [cost.to_dict() for cost in self.costs],
        }


# ---------------------------------------------------------------------------
# 8. 后果严重度：与"风险"互不重叠的两个轴
# ---------------------------------------------------------------------------
class ConsequenceSeverity(StrEnum):
    """后果落在角色身上的量级。

    **与 ``Risk`` 是两个轴**：``Risk`` 动的是"掷骰难不难"（DC / 优势），
    严重度动的是"失败之后有多疼"。风险同时抬这两个 = 同一个原因被计了两遍，
    而且玩家感觉不到"这次为什么这么疼"。
    """

    NONE = "NONE"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    SEVERE = "SEVERE"

    @property
    def rank(self) -> int:
        return _SEVERITY_ORDER.index(self)


_SEVERITY_ORDER: tuple[ConsequenceSeverity, ...] = (
    ConsequenceSeverity.NONE,
    ConsequenceSeverity.MINOR,
    ConsequenceSeverity.MAJOR,
    ConsequenceSeverity.SEVERE,
)


@dataclass(frozen=True, slots=True)
class SeverityScale:
    """基础严重度 + 每差一档抬几级。**从差多少派生，不从档位名派生。**

    ``per_step`` 默认 **0**：不声明就不升级。给个"合理默认"会让所有没写
    这条声明的检定突然开始产生后果严重度 —— 那是替规则作者改规则。
    """

    base: ConsequenceSeverity = ConsequenceSeverity.NONE
    per_step: int = 0

    def raise_by(self, steps: int) -> ConsequenceSeverity:
        index = self.base.rank + max(0, int(steps)) * self.per_step
        return _SEVERITY_ORDER[max(0, min(len(_SEVERITY_ORDER) - 1, index))]

    def resolve(self, *, degree_steps_below: int) -> ConsequenceSeverity:
        """``degree_steps_below``：1 = 刚好失败（最浅的失败），2 = 再差一档……

        减 1 是为了让 ``base`` 落在"刚好失败"上 —— 否则 ``base`` 永远不会
        被用到（``steps=0`` 意味着成功，而成功的后果无所谓严重度）。
        """

        return self.raise_by(max(0, int(degree_steps_below) - 1))


def steps_below_success(
    degrees: Sequence[tuple[str, bool]], degree_id: str,
) -> int:
    """比**最差的那个成功档**还差几级。0 = 成功或更好。

    ``degrees`` 是按"从好到坏"排列的 ``(id, achieved)``。用"差多少"而不是
    "叫什么名字"，是为了让同一个严重度公式能套在任何一套自定义档位名上。
    """

    worst_success = -1
    for index, (_id, achieved) in enumerate(degrees):
        if achieved:
            worst_success = index
    for index, (current_id, _achieved) in enumerate(degrees):
        if current_id == degree_id:
            return max(0, index - worst_success)
    return 0


def severity_for_degree(
    degree_id: str,
    degrees: Sequence[tuple[str, bool]],
    scale: SeverityScale,
    *,
    risk: Any = None,
) -> ConsequenceSeverity:
    """后果严重度 = 基础严重度 + 差了几档。**没失败就没有严重度。**

    ``risk`` 被**接受但忽略**：调用方手上通常正好有风险对象，如果签名里没有
    这个参数，调用方就会在外面自己抬一档 —— 那才是双重计价真正发生的地方。
    放在签名里并给出结论，会让"我该不该在这里再加一点"变成一个有答案的问题。
    """

    del risk  # 见 docstring：这是刻意的
    steps = steps_below_success(degrees, degree_id)
    if steps <= 0:
        # 目标达成了就没有"后果"这回事 —— "成功的后果有多严重"不是一个
        # 有意义的问题。部分达成也不算失败，它的程度由 ``extent`` 表达。
        return ConsequenceSeverity.NONE
    return scale.resolve(degree_steps_below=steps)


# ---------------------------------------------------------------------------
# 9. 规则追踪：这一次裁定里，哪条规则为什么触发了
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TraceEntry:
    """一个触发点。``rule`` 是来源标识，``kind`` 是阶段。"""

    rule: str
    kind: str
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "kind": self.kind,
            "reason": self.reason,
            "detail": deepcopy(self.detail),
        }


#: ``TraceEntry.kind`` 的取值。刻意保持很窄 —— 追踪条目一多就没人看了。
TRACE_KINDS = (
    "pre_roll",     # 骰前三问 / 尝试账本判定
    "roll",         # 掷骰与取目标数
    "degree",       # 档位判定
    "consumption",  # 代价
    "effect",       # 角色效果
    "world",        # 世界变更
    "attempt",      # 尝试记账
    "severity",     # 后果严重度
)


@dataclass(frozen=True, slots=True)
class RuleTrace:
    """一次裁定的完整推理链。

    存在的理由很实际：规则声明越来越厚之后，"为什么这次掷骰目标数是 39"
    光看结果根本答不上来。前端把它展开给玩家看，GM 模型靠它解释，
    规则作者靠它调试自己写的声明。
    """

    entries: tuple[TraceEntry, ...] = ()

    def add(
        self, rule: str, kind: str, reason: str = "", **detail: Any,
    ) -> RuleTrace:
        if kind not in TRACE_KINDS:
            raise AdjudicationError(
                f"未知的追踪阶段 {kind!r}（合法值：{', '.join(TRACE_KINDS)}）"
            )
        return RuleTrace((
            *self.entries,
            TraceEntry(rule=rule, kind=kind, reason=reason, detail=detail),
        ))

    def of_kind(self, kind: str) -> tuple[TraceEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == kind)

    def to_list(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.entries]

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)

