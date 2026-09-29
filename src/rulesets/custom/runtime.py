"""``RulesetRuntime`` 协议实现：自定义声明式规则。

这个类就是"你自己的规则大脑"。引擎只通过这些方法调用你：

    建卡   describe_experience / validate_character / derive_character
           / finalize_character / normalize_character_submission
    结算   available_intents / prepare_intent_submission / validate_intent
           / resolve_intent / apply_event_batch
    投影   gameplay_view（前端）/ build_llm_view（GM 模型）
    生命周期 migrate_state / project_legacy_character

两类运行模式（``AUTHORITATIVE_INTENTS`` 开关，**默认开启**）：

**Stage A（``authoritative_intents=False``，设 ``=0`` 退回）**
    完全不接管回合流水线。玩家照常自由文本行动，引擎走
    ``check_mechanic`` + ``engine/checks.py`` 的叙事检定路径。本运行时负责：

    - 把自定义资源状态注入 GM 上下文（``build_llm_view``）；
    - 阻止 LLM 通过叙事标签改写权威字段（``filter_narrative_state_update``）；
    - 新席位加入时派发初始资源（``on_player_join``）。

**Stage B（``authoritative_intents=True``，默认）**
    打开权威意图路径：``available_intents`` 暴露可点选动作，
    ``resolve_intent`` 用服务端 RNG 掷骰并产出 EventBatch，``apply_event_batch``
    落状态。因为 ``narrative_turns=True``，**日常仍是自由文本**，只有你主动把
    ``ruleset_state["combat"]["status"]`` 置为 ``"active"`` 才会强制结构化意图
    （D&D 2024 用的就是这个模式）。

它曾经是默认值，因为在补完前端 host 组件之前打开会让入局页直接报错；组件现在
在 ``frontend-v2/src/features/rulesets/custom/`` 下，所以默认翻转。
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from functools import lru_cache
from typing import Any
from uuid import uuid4

from src.rulesets.adjudication import (
    CONSUMPTION_FAILED,
    CostLine,
    OutcomeBreakdown,
    Resolution,
    RuleTrace,
    consumption_charges,
    consumption_checked_before_roll,
    effect_descriptors_from,
    intent_field_violations,
    severity_for_degree,
)
from src.rulesets.attempts import RetryPolicy
from src.rulesets.attempts import failure_sticks as evaluate_failure_sticks
from src.rulesets.attempts import record_attempt as record_attempt_entry
from src.rulesets.attempts import retry_verdict as evaluate_retry
from src.rulesets.attempts import world_versions as snapshot_world_versions
from src.rulesets.contracts import RulesetCapabilities
from src.rulesets.effects import (
    ActiveEffect,
    Change,
    advance_durations,
    effects_for,
    project as project_effects,
    upsert_effect,
)
from src.rulesets.world import apply_effects as apply_world_effects
from src.rulesets.custom import binding as custom_binding
from src.rulesets.custom import projection as custom_projection
from src.rulesets.custom import state as custom_state
from src.rulesets.custom.binding import (
    CONTENT_VERSION,
    MAX_EVENT_LEDGER,
    RUNTIME_ID,
    RUNTIME_VERSION,
    STATE_SCHEMA_VERSION,
    rule_binding,
)
from src.rulesets.custom.dice import parse_delta, resolve_check
from src.rulesets.custom.manifest import (
    EMPTY,
    CustomMechanics,
    parse_custom_mechanics,
)

logger = logging.getLogger("trpg")

# ---------------------------------------------------------------------------
# 模式开关：权威意图路径 + 专业建卡。**默认开启**。
#
# 这两项曾经默认关闭，因为打开后还需要两样东西才不成灾：
#   - 前端要能渲染 ``profile="custom"`` 的建卡器（否则入局页报
#     ``Unsupported ruleset experience``）；
#   - 前端要有能提交自定义意图的面板（否则点了没用）。
# 两者都已落地（``frontend-v2/src/features/rulesets/custom/``），所以默认翻转。
#
# **退回 Stage A**：设 ``=0`` 即可，但必须在服务启动之前设 ——
# 能力位在模块导入时求值一次。
#
#     PowerShell:  $env:DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS = "0"
#     bash:        export DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS=0
#
# 打开后还需要存档已绑定本运行时，否则 HTTP 接口报 ``RULESET_BINDING_MISMATCH``。
# 自测：``scripts/dev/test_stage_b.py``（离线，不需要服务/LLM/token）。
# 说明：``docs/STAGE_B_TEST_CN.md``。
# ---------------------------------------------------------------------------
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, *, default: bool = False) -> bool:
    """读布尔开关。

    **未设置或只写了空白**时用 ``default``；显式写了别的值就按真假集合解析 ——
    这样 ``=0`` / ``=false`` 永远是关，而不会因为"非空所以算开"变成反直觉的行为。
    """

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


AUTHORITATIVE_INTENTS = _env_flag(
    "DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS", default=True,
)

# 专业建卡（阶段 0）：打开后 ``character_builder`` 变 professional、
# ``character_lifecycle`` 变 rules_aware，存档才会被绑定、规则声明才会快照。
#
# **这两件事必须成对**：绑定只在 ``character_builder == "professional"`` 的
# 分支里写入，所以只开 ``AUTHORITATIVE_INTENTS`` 会得到
# ``RULESET_BINDING_MISMATCH``（有权威意图，但存档没绑定）。
#
# ``rules_aware`` 会禁用旧版通用角色编辑接口 —— 这是有意的：权威数值只能经
# 规则运行时变化。
PROFESSIONAL_BUILDER = _env_flag(
    "DICEFRAME_CUSTOM_PROFESSIONAL_BUILDER", default=True,
)

INTENT_CHECK = "custom.check.roll"
INTENT_ADJUST = "custom.resource.adjust"

#: **记录型事件**：进账本、进 reducer 输出供叙事与审计读取，但本身不改变状态。
#:
#: 它们必须在这里**显式声明**，不能靠"不认识就跳过"。区别很重要：
#:   - 显式声明 = 契约（叙事层可以依赖它存在）；
#:   - 静默跳过 = bug 温床（``custom.check.resolved`` 曾经就这样被丢掉，
#:     导致掷骰与成功度根本到不了叙事层）。
#:
#: 对齐 D&D 2024 的 reducer：``intent.submitted`` / ``check.resolved`` 等在那里
#: 也是列在 match 里的空操作（``dnd2024/combat/reducer.py``）。
RECORD_ONLY_EVENTS = frozenset({"custom.check.resolved"})

#: 尝试记录事件。还原器会把它**推到批次末尾**才执行：记录里的世界版本必须是
#: 本批世界变更落盘**之后**的版本，否则"失败本身改了目标"的尝试会把自己作废。
EVENT_ATTEMPT_RECORD = "custom.attempt.record"

#: 给行动者挂一个活跃效果。有效值在读取时算（base + effects），不写基础值。
EVENT_EFFECT_APPLIED = "custom.effect.applied"


@lru_cache(maxsize=64)
def _mechanics_from_json(raw: str) -> CustomMechanics:
    """按声明原文缓存解析结果，避免每轮重复校验。"""

    if raw == "null":
        return EMPTY
    return parse_custom_mechanics({"custom_mechanics": json.loads(raw)})


def _mechanics(rule: Any) -> CustomMechanics:
    template = getattr(rule, "template", None)
    if not isinstance(template, dict):
        return EMPTY
    raw = template.get("custom_mechanics")
    if raw is None:
        return EMPTY
    try:
        return _mechanics_from_json(json.dumps(raw, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        # 规则写错必须在运行时可见，而不是静默变成"不掷骰"。
        logger.error("custom_mechanics 声明非法，本局按空声明运行: %s", exc)
        return EMPTY


def _mechanics_from_declaration(declaration: Any) -> CustomMechanics | None:
    """把一份 ``custom_mechanics`` 声明原文解析成 ``CustomMechanics``。"""

    if not isinstance(declaration, dict):
        return None
    try:
        return _mechanics_from_json(
            json.dumps(declaration, sort_keys=True, ensure_ascii=False)
        )
    except (TypeError, ValueError) as exc:
        logger.error("规则声明快照非法，回退到规则对象: %s", exc)
        return None


def _declaration_from_sheet(instance: Any, uid: str = "") -> dict[str, Any] | None:
    """从席位角色卡里取规则声明快照。

    规则声明随角色卡一起走：``character_sheet.ruleset_character.mechanics``
    （写入点 ``src/webui/services/characters.py`` 组装 ``cs`` 处）。

    这里是**只读自愈**通道：老存档没有 ``ruleset_state["mechanics"]`` 时用它
    顶一下，但不落盘 —— 游戏期方法可能在写锁之外被调用，不能在那里改状态。
    真正落盘由 ``on_player_join`` 负责。
    """

    players = getattr(instance, "players", None)
    if not isinstance(players, dict) or not players:
        return None
    order = [uid] if uid and uid in players else []
    order += [key for key in players if key not in order]
    for key in order:
        sheet = CustomDeclarativeRuntime._sheet(instance, str(key))
        if not isinstance(sheet, dict):
            continue
        canonical = sheet.get("ruleset_character")
        if not isinstance(canonical, dict):
            continue
        declaration = canonical.get("mechanics")
        if isinstance(declaration, dict):
            return declaration
    return None


def _mechanics_for(instance: Any, uid: str = "") -> CustomMechanics:
    """游戏期取 mechanics，按可信度降序回退。

    1. ``ruleset_state["mechanics"]`` —— 权威快照，由 ``on_player_join`` 落盘；
    2. 席位角色卡里的声明 —— 老存档自愈（只读）；
    3. 宿主传入的规则对象 —— 建卡流程 / 测试夹具。

    协议只在建卡方法里把 ``rule`` 交给运行时，而游戏期方法拿不到 ``rule``，
    所以 1 和 2 才是主通道（见 ``docs/STAGE_B_TEST_CN.md``）。
    """

    state = getattr(instance, "ruleset_state", None)
    declaration = state.get("mechanics") if isinstance(state, dict) else None
    resolved = _mechanics_from_declaration(declaration)
    if resolved is not None:
        return resolved
    resolved = _mechanics_from_declaration(_declaration_from_sheet(instance, uid))
    if resolved is not None:
        return resolved
    return _mechanics(CustomDeclarativeRuntime._rule(instance))


# ---------------------------------------------------------------------------
# 建卡素材读取：通用规则模板字段 + custom_mechanics 声明
#
# 刻意分成两处而不是把属性定义搬进 custom_mechanics —— 引擎侧
# （``_get_rule_attrs_for_game`` / ``normalize_character_sheet`` /
# ``build_starter_items``）读的就是通用模板字段，搬过来会造成第二处真相。
# ---------------------------------------------------------------------------


def _rule_template(rule: Any) -> dict[str, Any]:
    template = getattr(rule, "template", None)
    return template if isinstance(template, dict) else {}


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _attribute_specs(rule: Any) -> list[dict[str, Any]]:
    """规则模板声明的属性行：``[{"key","name","min","max"}]``。"""

    rows = _rule_template(rule).get("attributes")
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows if isinstance(row, dict) and str(row.get("key") or "")
    ]


def _attribute_points(rule: Any) -> int:
    """属性点购预算（模板 ``attribute_points``）。0 表示不限制。"""

    return max(0, _int_or(_rule_template(rule).get("attribute_points"), 0))


def _name_rows(value: Any) -> list[dict[str, str]]:
    """把模板里的列表/字典选项投影成统一的 ``{ref, name}`` 行。"""

    rows: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict):
                rows.append({"ref": str(key), "name": str(item.get("name") or key)})
            elif isinstance(item, str):
                rows.append({"ref": str(key), "name": item})
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                ref = str(item.get("key") or item.get("ref") or item.get("id") or index)
                rows.append({"ref": ref, "name": str(item.get("name") or ref)})
            elif isinstance(item, str):
                rows.append({"ref": item, "name": item})
    return rows


def _distribute(
    specs: list[dict[str, Any]], budget: int, weights: list[int],
) -> dict[str, int]:
    """把 ``budget`` 按 ``weights`` 分摊到各属性，并夹在 [min, max] 内。

    分摊不成功（预算不可达）时返回空 dict —— 宁可不给预设，也不给一张
    ``validate_character`` 会拒绝的卡。
    """

    if not specs or budget <= 0 or len(weights) != len(specs):
        return {}
    total_weight = sum(weights)
    if total_weight <= 0:
        return {}
    bounds = [
        (_int_or(spec.get("min"), 3), _int_or(spec.get("max"), 18)) for spec in specs
    ]
    allocated = [
        max(low, budget * weight // total_weight)
        for weight, (low, _high) in zip(weights, bounds)
    ]
    # 修补取整误差：多了从余量最大的削，少了给余量最大的补。
    for _ in range(512):
        total = sum(allocated)
        if total == budget:
            break
        if total > budget:
            candidates = [
                i for i in range(len(allocated)) if allocated[i] > bounds[i][0]
            ]
            if not candidates:
                break
            index = max(
                candidates, key=lambda i: (allocated[i] - bounds[i][0], -i),
            )
            allocated[index] -= 1
        else:
            candidates = [
                i for i in range(len(allocated)) if allocated[i] < bounds[i][1]
            ]
            if not candidates:
                break
            index = max(
                candidates, key=lambda i: (bounds[i][1] - allocated[i], -i),
            )
            allocated[index] += 1
    if sum(allocated) != budget:
        return {}
    return {specs[i]["key"]: allocated[i] for i in range(len(specs))}


def _quick_presets(
    specs: list[dict[str, Any]], budget: int, template: dict[str, Any],
) -> list[dict[str, Any]]:
    """内置两档预设：均衡与偏科。draft 可直接喂给 ``finalize_character``。"""

    presets: list[dict[str, Any]] = []
    balanced = _distribute(specs, budget, [1] * len(specs))
    if balanced:
        presets.append({
            "id": "balanced",
            "name": "均衡",
            "difficulty": "normal",
            "fantasy_tags": ["通用", "新手友好"],
            "recommendation_reason": "各属性平均分配，适合自由团和新玩家。",
            "draft": {"attributes": balanced},
        })
    if len(specs) >= 3:
        weights = [3, 3] + [1] * (len(specs) - 2)
        focused = _distribute(specs, budget, weights)
        if focused and focused != balanced:
            presets.append({
                "id": "focused",
                "name": "偏科",
                "difficulty": "normal",
                "fantasy_tags": ["专精"],
                "recommendation_reason": "前两项主属性拉高，其余压近下限，突出角色定位。",
                "draft": {"attributes": focused},
            })
    if not presets and template.get("attribute_default") is not None:
        return []
    return presets


def _int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, item in value.items():
        try:
            result[str(key)] = int(item)
        except (TypeError, ValueError):
            continue
    return result


def _skill_names(source: dict[str, Any]) -> list[Any]:
    skills = source.get("skills")
    if isinstance(skills, list):
        return deepcopy(skills)
    if isinstance(skills, dict):
        return [str(key) for key in skills]
    if isinstance(skills, str) and skills.strip():
        return [skills.strip()]
    return []


def _hit_points(
    rule: Any, attributes: dict[str, int], class_name: str,
) -> tuple[int, int]:
    """用规则自带的 ``hp_formula`` 算 HP；规则写坏时退回 10 并留下日志。"""

    calculate = getattr(rule, "calculate_hp", None)
    if callable(calculate):
        try:
            value = int(calculate(attributes, class_name))
            if value > 0:
                return value, value
        except Exception as exc:  # noqa: BLE001 - hp_formula 写坏必须可见
            logger.error("hp_formula 求值失败，退回默认 HP: %s", exc)
    return 10, 10


def _armor_class(template: dict[str, Any]) -> int:
    """本期只支持模板声明的固定基础 AC。

    声明式规则目前没有护甲模型（示例规则用 ``lethal_narrative``），所以不去
    发明公式；需要时由模板 ``base_armor_class`` 覆盖。``characters.py`` 无条件
    读 ``character.get("armor_class", 10)``，所以这个键必须有。
    """

    return _int_or(template.get("base_armor_class"), 10)


def _character_binding(rule: Any) -> dict[str, Any]:
    """角色卡上携带的规则绑定（5 字段，比引擎落盘的 4 字段多一个 ``rule_id``）。

    ``rule_id`` 是必要的：``ruleset_characters.runtime_for_card`` 在没有 binding
    时要回退读 ``card["rule_id"]``。
    """

    return {
        "rule_id": str(getattr(rule, "rule_id", "") or ""),
        "runtime_id": RUNTIME_ID,
        "runtime_version": RUNTIME_VERSION,
        "content_version": CONTENT_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
    }


def _mechanics_seed(rule: Any) -> dict[str, Any]:
    """要随角色卡一起走的 ``custom_mechanics`` 声明原文。"""

    declaration = _rule_template(rule).get("custom_mechanics")
    return deepcopy(declaration) if isinstance(declaration, dict) else {}


def _draft_from_canonical(source: dict[str, Any]) -> dict[str, Any]:
    """从 canonical 或客户端卡里**只取出"选择"**，丢掉所有派生数值。

    这是"客户端不能伪造数值"的落点：``hp`` / ``max_hp`` / ``armor_class`` 之类
    在这里就不进入 draft，后面由 ``derive_character`` 重算。
    """

    identity = source.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    abilities = source.get("abilities")
    if not isinstance(abilities, dict):
        abilities = source.get("attributes")
    return {
        "name": str(
            identity.get("name") or source.get("name")
            or source.get("character_name") or ""
        ),
        "race": str(identity.get("race") or source.get("race") or ""),
        "class": str(identity.get("class") or source.get("class") or ""),
        "background": str(identity.get("background") or source.get("background") or ""),
        "attributes": _int_map(abilities),
        "skills": _skill_names(source),
        "locale": str(source.get("locale") or ""),
    }


def _world_effect_payload(spec: Any, rng: Any) -> dict[str, Any]:
    """把一条世界效果声明变成事件 payload。

    ``delta``（骰式）在这里用**注入的服务端 RNG** 解析成具体数值 ——
    世界状态层拒绝公式，因为它拿不到 RNG。
    """

    return {
        "kind": "world",
        "target": spec.target,
        "field": spec.field,
        "op": spec.op,
        "value": (
            spec.value if spec.op == "override" or not spec.delta
            else parse_delta(rng, spec.delta)
        ),
    }


def _short_circuit_batch(
    actor: str, intent_type: str, uid: str, check: Any,
    verdict: tuple[Any, list[str], str],
    extra: list[dict[str, Any]] | None = None,
    trace: RuleTrace | None = None,
) -> dict[str, Any]:
    """不掷骰的裁定：记下裁定本身、推理链，外加**骰前就该发生的**代价。

    **刻意不产成功度效果**：``AUTO_SUCCESS`` 该落哪个档位的效果，是规则作者
    该表态的事（需要类似 ``auto_success_degree`` 的声明）；现在自己挑一档
    就是发明规则。

    但 `ON_DECLARE` 的消耗不在此列 —— 它与成功度无关，"我试了"这件事
    已经发生了，代价就该付。这也是三种时机里唯一在短路路径上可观测的区别。

    事件类型用既有的 ``custom.check.resolved``（在 reducer 里是记录型事件），
    所以不打破任何现有路径，叙事层靠 ``resolution`` 与 ``reason`` 工作。
    """

    resolution, reasons, _rule = verdict
    record: dict[str, Any] = {
        "type": "custom.check.resolved",
        "actor_id": uid,
        "check_id": str(getattr(check, "id", "")),
        "resolution": str(resolution),
        "reason": list(reasons),
    }
    if trace:
        record["trace"] = trace.to_list()
    return {
        "ok": True,
        "event_batch": {
            "batch_id": uuid4().hex,
            "intent_type": intent_type,
            "actor_id": actor,
            "events": [*list(extra or ()), record],
        },
        "replayed": False,
        "pending_decision": None,
    }


def _find_degree(check: Any, degree_id: str) -> Any:
    for degree in getattr(check, "degrees", ()) or ():
        if degree.id == degree_id:
            return degree
    return None


def _seed_world(raw_world: dict[str, Any], seed: dict[str, dict[str, Any]]) -> None:
    """首次用到世界状态时插入初始对象；已存在的**不覆盖**。

    不覆盖是必须的：否则已经解锁的门会在下一批又被规则声明重新锁上。
    """

    for object_id, payload in seed.items():
        if object_id not in raw_world:
            raw_world[object_id] = deepcopy(payload)


class CustomDeclarativeRuntime:
    """自定义声明式规则的运行时实现。"""

    runtime_id = RUNTIME_ID
    runtime_version = RUNTIME_VERSION
    state_schema_version = STATE_SCHEMA_VERSION
    capabilities = RulesetCapabilities(
        experience_profile="custom",
        character_builder="professional" if PROFESSIONAL_BUILDER else "guided",
        character_lifecycle="rules_aware" if PROFESSIONAL_BUILDER else "legacy",
        authoritative_intents=AUTHORITATIVE_INTENTS,
        narrative_turns=True,
        deterministic_combat=False,
        versioned_state=False,
    )

    # ------------------------------------------------------------------
    # 建卡
    # ------------------------------------------------------------------

    def describe_experience(self, rule: Any, locale: str) -> dict[str, Any]:
        del locale
        mechanics = _mechanics(rule)
        return {
            "profile": "custom",
            "rule_id": str(getattr(rule, "rule_id", "") or ""),
            "builder_mode": self.capabilities.character_builder,
            "runtime_version": self.runtime_version,
            "content_version": custom_binding.CONTENT_VERSION,
            "authoritative_intents": self.capabilities.authoritative_intents,
            "resources": [
                {"id": spec.id, "name": spec.name} for spec in mechanics.resources
            ],
            "checks": [
                {"id": check.id, "name": check.name, "dice": check.dice}
                for check in mechanics.checks
            ],
        }

    def builder_choices(self, rule: Any, draft: dict[str, Any]) -> dict[str, Any]:
        """建卡选项：属性范围、点购预算、资源、检定与快速预设。

        ``quick_presets[*]["draft"]`` 必须能**原样**喂给 ``finalize_character``：
        前端的"一键建卡"和测试夹具共用这条路径。
        """

        del draft
        template = _rule_template(rule)
        mechanics = _mechanics(rule)
        specs = _attribute_specs(rule)
        budget = _attribute_points(rule)
        return {
            "profile": "custom",
            "attributes": [
                {
                    "key": spec["key"],
                    "name": str(spec.get("name") or spec["key"]),
                    "min": _int_or(spec.get("min"), 3),
                    "max": _int_or(spec.get("max"), 18),
                }
                for spec in specs
            ],
            "attribute_points": budget,
            "attribute_default": _int_or(template.get("attribute_default"), 10),
            "attr_hint": str(template.get("attr_hint") or ""),
            "races": _name_rows(template.get("races")),
            "classes": _name_rows(template.get("classes")),
            "skills": _name_rows(template.get("skills")),
            "max_skills": _int_or(template.get("max_skills"), 0),
            "skill_hint": str(template.get("skill_hint") or ""),
            "resources": [
                {"id": spec.id, "name": spec.name, "default": spec.default}
                for spec in mechanics.resources
            ],
            "checks": [
                {"id": check.id, "name": check.name, "dice": check.dice}
                for check in mechanics.checks
            ],
            "quick_presets": _quick_presets(specs, budget, template),
        }

    def validate_character(self, rule: Any, draft: dict[str, Any]) -> list[str]:
        """返回错误消息列表，空列表表示合法。

        刻意返回 ``list[str]`` 而不是 bool —— ``ruleset_builder`` 靠 ``if errors``
        判空。

        复用引擎的通用校验（属性/技能/背景长度等），**再叠加**声明式规则独有的
        机制校验（属性范围与点购预算）。刻意不去重写通用部分，避免出现第二处
        属性真相。
        """

        draft = draft if isinstance(draft, dict) else {}
        errors: list[str] = []
        generic = getattr(rule, "validate_character", None)
        if callable(generic):
            try:
                errors.extend(str(item) for item in (generic(draft) or []))
            except Exception as exc:  # noqa: BLE001 - 规则校验器坏掉要可见
                logger.error("通用角色校验失败，跳过: %s", exc)

        specs = _attribute_specs(rule)
        attributes = draft.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        for spec in specs:
            key = spec["key"]
            label = str(spec.get("name") or key)
            if key not in attributes:
                errors.append(f"缺少属性：{label}")
                continue
            try:
                value = int(attributes[key])
            except (TypeError, ValueError):
                errors.append(f"属性 {label} 必须是整数")
                continue
            low = _int_or(spec.get("min"), 3)
            high = _int_or(spec.get("max"), 18)
            if not low <= value <= high:
                errors.append(f"属性 {label} 超出允许范围 {low}-{high}")

        budget = _attribute_points(rule)
        if budget and specs and all(spec["key"] in attributes for spec in specs):
            try:
                total = sum(int(attributes[spec["key"]]) for spec in specs)
            except (TypeError, ValueError):
                total = 0
            if total > budget:
                errors.append(f"属性总点 {total} 超过上限 {budget}")
        return errors

    def derive_character(self, rule: Any, draft: dict[str, Any]) -> dict[str, Any]:
        """产出 canonical ``ruleset_character``。

        **有错必须 raise ValueError** —— ``ruleset_builder`` 与
        ``characters.create_player`` 都靠捕获它来返回 ``INVALID_DRAFT`` /
        ``INVALID_PROFESSIONAL_CHARACTER``。

        **刻意不在这里解析资源数值**：资源由 ``on_player_join`` 经
        ``custom_state.derive_resources`` 派生，避免 ``ValueRef`` 求值有两处实现。
        canonical 只带 ``mechanics`` 声明原文，让游戏期能自己算。
        """

        errors = self.validate_character(rule, draft)
        if errors:
            raise ValueError("; ".join(errors))
        abilities = _int_map(draft.get("attributes"))
        identity = {
            "name": str(draft.get("name") or draft.get("character_name") or "冒险者"),
            "race": str(draft.get("race") or ""),
            "class": str(draft.get("class") or ""),
            "background": str(draft.get("background") or ""),
        }
        hp, max_hp = _hit_points(rule, abilities, identity["class"])
        return {
            "rule_binding": _character_binding(rule),
            "mechanics": _mechanics_seed(rule),
            "locale": str(draft.get("locale") or ""),
            "identity": identity,
            "abilities": abilities,
            "skills": _skill_names(draft),
            "derived": {
                "hp": hp,
                "max_hp": max_hp,
                "armor_class": _armor_class(_rule_template(rule)),
            },
        }

    def finalize_character(self, rule: Any, draft: dict[str, Any]) -> dict[str, Any]:
        """建卡器最终产出。**三个键缺一不可**。

        ``src/webui/services/characters.py`` 读的是
        ``character["ruleset_character"]["rule_binding"]`` 并拿它去
        ``bind_ruleset_runtime``；少一个键就会得到
        ``INCOMPATIBLE_RULESET_CHARACTER`` —— 建卡成功但存档永不绑定。
        """

        canonical = self.derive_character(rule, draft)
        return {
            **self.project_legacy_character(canonical),
            "rule_binding": deepcopy(canonical["rule_binding"]),
            "ruleset_character": canonical,
        }

    def normalize_character_submission(
        self, rule: Any, character: dict[str, Any], locale: str = "",
    ) -> dict[str, Any]:
        """归一化客户端提交的角色卡（返回与 ``finalize_character`` 同形状）。

        三条硬约束，破坏任一条都会让门控**静默失效**：

        1. **不信任派生值** —— hp / max_hp / armor_class 一律重算，客户端提交的
           被丢弃（参见 ``tests/test_webui_characters.py`` 用 999 做的断言）；
        2. **binding 逐字段匹配** —— 版本不符必须 ``ValueError``，否则旧卡能混进
           新规则的对局；
        3. **canonical 必须带 ``mechanics``** —— 这是规则声明进入
           ``ruleset_state`` 的唯一来源（见 ``on_player_join``）。

        入口有两处且都要求幂等：``characters.create_player``（建人入局）与
        ``game_lifecycle`` / ``game_seed_lifecycle``（开局批次预检，任一张卡失败
        则整批拒绝且不留幻影对局）。
        """

        incoming = character.get("ruleset_character")
        if isinstance(incoming, dict):
            expected = _character_binding(rule)
            got = incoming.get("rule_binding")
            if not isinstance(got, dict):
                raise ValueError("专业角色卡缺少 rule_binding，无法确认规则版本")
            for key in (
                "runtime_id", "runtime_version", "content_version", "state_schema_version",
            ):
                if key in got and got[key] != expected[key]:
                    raise ValueError(
                        f"专业角色卡与当前规则版本不兼容（{key}: "
                        f"{got[key]!r} != {expected[key]!r}）"
                    )
            source = incoming
        else:
            source = character if isinstance(character, dict) else {}

        draft = _draft_from_canonical(source)
        if not draft.get("locale"):
            draft["locale"] = str(locale or "")
        canonical = self.derive_character(rule, draft)
        return {
            **self.project_legacy_character(canonical),
            "rule_binding": deepcopy(canonical["rule_binding"]),
            "ruleset_character": canonical,
        }

    def project_legacy_character(self, character: dict[str, Any]) -> dict[str, Any]:
        """把 canonical 投影成引擎认识的**扁平**角色卡字段。

        ``characters.py`` 组装 ``character_sheet`` 时读的是这些平坦键，所以必须
        覆盖它们；同时兼容"外面还套着 ``ruleset_character``"的形状 ——
        ``ruleset_characters._apply_profile`` 就是那样调的。
        """

        canonical = character.get("ruleset_character")
        if not isinstance(canonical, dict):
            canonical = character if isinstance(character, dict) else {}
        identity = canonical.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        derived = canonical.get("derived")
        derived = derived if isinstance(derived, dict) else {}
        hp = _int_or(derived.get("hp"), 0)
        return {
            "character_name": str(identity.get("name") or "冒险者"),
            "race": str(identity.get("race") or ""),
            "class": str(identity.get("class") or ""),
            "background": str(identity.get("background") or ""),
            "attributes": deepcopy(canonical.get("abilities") or {}),
            "skills": deepcopy(canonical.get("skills") or []),
            "hp": hp,
            "max_hp": _int_or(derived.get("max_hp"), hp),
            "armor_class": _int_or(derived.get("armor_class"), 10),
        }

    # ------------------------------------------------------------------
    # 权威意图（Stage B）
    # ------------------------------------------------------------------

    def available_intents(self, instance: Any, actor_id: str) -> list[dict[str, Any]]:
        """暴露给前端的可点选动作。

        只有当席位存在时才给动作；资源调整只在 GM 视图暴露（玩家改自己的数值
        必须走检定或 GM 裁定，不能自己点）。
        """

        if not self.capabilities.authoritative_intents:
            return []
        mechanics = _mechanics_for(instance)
        if not isinstance(getattr(instance, "players", None), dict):
            return []
        if str(actor_id or "") not in instance.players:
            return []
        intents: list[dict[str, Any]] = [
            {
                "type": INTENT_CHECK,
                "check_id": check.id,
                "label": check.name,
                "dice": check.dice,
            }
            for check in mechanics.checks
        ]
        if str(actor_id or "") == str(getattr(instance, "gm_uid", "") or ""):
            intents.extend(
                {
                    "type": INTENT_ADJUST,
                    "resource_id": spec.id,
                    "label": f"调整{spec.name}",
                }
                for spec in mechanics.resources
            )
        return intents

    def prepare_intent_submission(
        self, intent: dict[str, Any], requester_id: str, requester_is_gm: bool,
    ) -> dict[str, Any]:
        """服务端补齐意图身份。前端传来的 ``actor_id`` 一律不可信。"""

        prepared = deepcopy(intent)
        if not requester_is_gm:
            prepared["actor_id"] = f"player:{requester_id}"
        elif not str(prepared.get("actor_id") or "").strip():
            prepared["actor_id"] = f"player:{requester_id}"
        return prepared

    def validate_intent(self, instance: Any, intent: dict[str, Any]) -> dict[str, Any]:
        intent_type = str(intent.get("type") or "")
        if intent_type not in {INTENT_CHECK, INTENT_ADJUST}:
            return {
                "ok": False, "code": "UNKNOWN_INTENT",
                "error": f"未声明的意图类型: {intent_type!r}",
            }
        # 硬约束：客户端只能声明"想做什么"，不能声明"掷出了什么"。
        # 骰子由注入的服务端 RNG 产生，见 ``src/rulesets/adjudication.py``。
        violations = intent_field_violations(intent)
        if violations:
            return {
                "ok": False,
                "code": "CLIENT_ROLL_FORBIDDEN",
                "error": "意图不得包含掷骰结果字段：" + "、".join(violations),
            }
        actor = str(intent.get("actor_id") or "")
        if actor.startswith("player:"):
            uid = actor[len("player:"):]
            players = getattr(instance, "players", {}) or {}
            if uid not in players:
                return {"ok": False, "code": "ACTOR_NOT_IN_GAME", "error": "行动者不在本局中"}
        elif actor:
            return {"ok": False, "code": "INVALID_ACTOR", "error": "行动者身份不合法"}
        mechanics = _mechanics_for(instance)
        if intent_type == INTENT_CHECK:
            if _find_check(mechanics, str(intent.get("check_id") or "")) is None:
                return {"ok": False, "code": "UNKNOWN_CHECK", "error": "未声明的检定"}
        else:
            if _find_resource(mechanics, str(intent.get("resource_id") or "")) is None:
                return {"ok": False, "code": "UNKNOWN_RESOURCE", "error": "未声明的资源"}
        return {"ok": True}

    def resolve_intent(
        self, instance: Any, intent: dict[str, Any], rng: Any,
    ) -> dict[str, Any]:
        """掷骰 / 计算，产出 EventBatch。**这里不写状态**。"""

        verdict = self.validate_intent(instance, intent)
        if not verdict.get("ok"):
            return verdict
        mechanics = _mechanics_for(instance)
        actor = str(intent.get("actor_id") or "")
        uid = actor[len("player:"):] if actor.startswith("player:") else ""
        intent_type = str(intent.get("type") or "")
        events: list[dict[str, Any]] = []

        if intent_type == INTENT_CHECK:
            check = _find_check(mechanics, str(intent.get("check_id") or ""))
            if check is None:  # validate_intent 已挡；保持 fail-closed。
                return {"ok": False, "code": "UNKNOWN_CHECK", "error": "未声明的检定"}
            consumptions = mechanics.consumptions_for(check.id)
            trace = RuleTrace()
            # 骰前可负担性检查：不能先掷骰再发现扣不起 —— 那就等于白赚一次掷骰，
            # 而且玩家已经看到结果了，再告诉他"其实不行"是无法接受的。
            short_of = self._unaffordable(instance, uid, consumptions)
            if short_of is not None:
                resource_id, amount, have = short_of
                return {
                    "ok": False,
                    "code": CONSUMPTION_FAILED,
                    "error": (
                        f"资源 {resource_id} 不足：需要 {amount}，只有 {have}"
                    ),
                }
            for spec in consumptions:
                if consumption_checked_before_roll(spec.timing):
                    trace = trace.add(
                        f"consumption:{spec.resource}", "pre_roll",
                        f"{spec.timing} 的代价在掷骰前就要付，已确认付得起",
                        timing=spec.timing, amount=spec.amount,
                    )
            short = self._pre_roll_verdict(instance, mechanics, check, uid)
            if short is not None:
                # 不掷骰。ON_DECLARE 仍要付（"我试了"已经发生），其余不付。
                resolution, reasons, rule = short
                trace = trace.add(
                    rule, "pre_roll", "; ".join(reasons),
                    resolution=str(resolution),
                )
                extra, _costs, trace = self._consumption_events(
                    mechanics, check, uid,
                    rolled=False, succeeded=False, trace=trace,
                )
                return _short_circuit_batch(
                    actor, intent_type, uid, check, short,
                    extra=extra, trace=trace,
                )
            # 取**有效值**而不是基础值：挂着的效果必须真的影响这次检定的目标数，
            # 否则"效果生效了"就只是个账面数字。
            resources = self._effective_resources(instance, uid)
            target = custom_state.resolve_target(
                mechanics, self._sheet(instance, uid), ref=check.target, resources=resources,
            )
            trace = trace.add(
                "check.target", "roll",
                f"目标数 {target}（取自有效值：基础值 + 活跃效果）",
                check_id=check.id, target=target, dice=check.dice,
                resources=deepcopy(resources),
            )
            outcome = resolve_check(rng, check, target=target)
            degree = _find_degree(check, outcome.degree_id)
            achieved = degree.goal_achieved if degree is not None else outcome.is_success
            extent = degree.goal_extent if degree is not None else (1.0 if achieved else 0.0)
            severity = severity_for_degree(
                outcome.degree_id, check.degree_order, check.severity,
            )
            ratio = (outcome.roll.total / target) if target else 0.0
            trace = trace.add(
                "check.degree", "degree",
                f"{outcome.roll.total} / {target} = {ratio:.2f} → {outcome.degree_label}",
                degree=outcome.degree_id, degree_label=outcome.degree_label,
                achieved=achieved, extent=extent, severity=str(severity),
            )
            # 消耗排在奖励前面：**先付钱再拿东西**。反过来的话，资源已满时
            # 奖励会被上限截掉一段，紧接着再扣，就白亏了。
            consumption_events, cost_lines, trace = self._consumption_events(
                mechanics, check, uid, rolled=True, succeeded=achieved, trace=trace,
            )
            breakdown = OutcomeBreakdown.from_degree(
                degree_id=outcome.degree_id,
                degree_label=outcome.degree_label,
                resolution=Resolution.CHECK_REQUIRED,
                achieved=achieved,
                extent=extent,
                severity=severity,
            ).with_costs(cost_lines)
            events.append({
                "type": "custom.check.resolved",
                "actor_id": uid,
                **outcome.to_dict(),
                # 结果分解：成没成 / 成多少 / 有多疼 / 付了什么。
                "outcome": breakdown.to_dict(),
                # 推理链：这一次裁定里哪条规则为什么触发。
                "trace": trace.to_list(),
            })
            events.extend(consumption_events)
            for effect in mechanics.effects:
                if effect.check != check.id or effect.degree != outcome.degree_id:
                    continue
                delta = parse_delta(rng, effect.delta)
                if delta == 0:
                    continue
                events.append({
                    "type": "custom.resource.changed",
                    "actor_id": uid,
                    "resource_id": effect.resource,
                    "delta": delta,
                    "reason": f"{check.name}:{outcome.degree_label}",
                })
            for spec in mechanics.character_effects:
                if spec.check != check.id or spec.degree != outcome.degree_id:
                    continue
                events.append({
                    "type": EVENT_EFFECT_APPLIED,
                    "actor_id": uid,
                    "effect_id": spec.id,
                    "source": f"{check.name}:{outcome.degree_label}",
                    "label": spec.label,
                    "changes": [
                        {"key": key, "op": op, "value": value}
                        for key, op, value in spec.changes
                    ],
                    "duration": deepcopy(spec.duration),
                })
            world_effects: list[dict[str, Any]] = []
            attempt_events: list[dict[str, Any]] = []
            seen_attempts: set[tuple[str, ...]] = set()
            for spec in mechanics.world_effects:
                if spec.check != check.id or spec.degree != outcome.degree_id:
                    continue
                world_effects.append(_world_effect_payload(spec, rng))
                if not spec.intent_family:
                    continue
                signature = (
                    spec.intent_family, spec.approach_signature,
                    spec.retry_policy, spec.target,
                )
                if signature in seen_attempts:
                    continue
                seen_attempts.add(signature)
                attempt_events.append({
                    "type": EVENT_ATTEMPT_RECORD,
                    "actor_id": uid,
                    "intent_family": spec.intent_family,
                    "approach_signature": spec.approach_signature,
                    "retry_policy": spec.retry_policy,
                    "outcome": outcome.degree_id,
                    "target": spec.target,
                })
            if world_effects:
                # 一次检定只产一条 world.changed，里面包住所有世界变更 ——
                # 叙事层与审计看到的是"这次裁定改了什么"，不是一堆碎片。
                events.append({
                    "type": "world.changed",
                    "actor_id": uid,
                    "reason": f"{check.name}:{outcome.degree_label}",
                    "effects": world_effects,
                })
            # 尝试记录放在最后：世界变更先落，版本才有意义。
            events.extend(attempt_events)
        else:
            resource = _find_resource(mechanics, str(intent.get("resource_id") or ""))
            if resource is None:
                return {"ok": False, "code": "UNKNOWN_RESOURCE", "error": "未声明的资源"}
            try:
                delta = int(intent.get("delta", 0) or 0)
            except (TypeError, ValueError):
                return {"ok": False, "code": "INVALID_DELTA", "error": "增量必须是整数"}
            if delta == 0:
                return {"ok": False, "code": "INVALID_DELTA", "error": "增量不能为 0"}
            events.append({
                "type": "custom.resource.changed",
                "actor_id": uid,
                "resource_id": resource.id,
                "delta": delta,
                "reason": str(intent.get("reason") or "GM 裁定")[:64],
            })

        return {
            "ok": True,
            "event_batch": {
                "batch_id": uuid4().hex,
                "intent_type": intent_type,
                "actor_id": actor,
                "events": events,
            },
            "replayed": False,
            "pending_decision": None,
        }

    def apply_event_batch(
        self, instance: Any, batch: dict[str, Any],
    ) -> dict[str, Any]:
        """把 EventBatch 落到 ``ruleset_state``。**唯一的状态写入口**。

        失败语义是 **fail-closed**：任何不认识 / 不完整的事件都直接抛错，
        让整批由调用方回滚 —— **不静默跳过**。

        「全部成功或全部不发生」由宿主保证：``submit_intent`` 在
        ``instance._lock`` 内先快照 ``before``，异常时调
        ``instance.restore_ruleset_transaction(before)``。所以这里不做半回滚，
        否则会出现两套回滚语义。参考 D&D 2024 的 reducer：不认识的事件类型
        抛 ``EventBatchError``。
        """

        events = batch.get("events")
        if not isinstance(events, list):
            raise ValueError("event batch 缺少 events 数组")
        state = custom_state.read_state(instance)
        mechanics = _mechanics_for(instance)
        applied_events: list[dict[str, Any]] = []
        recorded_events: list[dict[str, Any]] = []
        deferred_attempts: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                raise ValueError(
                    f"event batch 里混入了非对象事件：{type(event).__name__}"
                )
            event_type = str(event.get("type") or "")
            if event_type in RECORD_ONLY_EVENTS:
                recorded_events.append(deepcopy(event))
                continue
            if event_type == EVENT_ATTEMPT_RECORD:
                # 刻意不在循环里立即写：记录里的世界版本必须是**本批变更落盘
                # 之后**的版本，所以推到循环外、世界变更都落完之后再处理。
                deferred_attempts.append(dict(event))
                continue
            if event_type == EVENT_EFFECT_APPLIED:
                ledger = state.setdefault("effects", {})
                if not isinstance(ledger, dict):
                    raise ValueError(
                        "ruleset_state.effects 必须是 {uid: [效果]} 形式的对象"
                    )
                uid = str(event.get("actor_id") or "")
                if not uid:
                    raise ValueError("custom.effect.applied 缺少 actor_id")
                effect_id = str(event.get("effect_id") or "")
                raw_changes = event.get("changes")
                if not isinstance(raw_changes, list) or not raw_changes:
                    raise ValueError(f"效果 {effect_id!r} 缺少 changes")
                # upsert 而非 append：同一个效果被描述两次是**续期**，不是叠层。
                # 叠层会让"这个 +2 哪来的"无法回答。
                written = upsert_effect(ledger, ActiveEffect(
                    id=effect_id,
                    target_uid=uid,
                    changes=tuple(
                        Change(
                            str(change.get("key") or ""),
                            str(change.get("op") or ""),
                            change.get("value"),
                        )
                        for change in raw_changes
                        if isinstance(change, dict)
                    ),
                    source=str(event.get("source") or ""),
                    note=str(event.get("label") or ""),
                    duration=(
                        deepcopy(event.get("duration"))
                        if isinstance(event.get("duration"), dict)
                        else None
                    ),
                ))
                applied_events.append({
                    "type": EVENT_EFFECT_APPLIED,
                    "actor_id": uid,
                    "effect_id": written["id"],
                    "changes": written["changes"],
                    "source": written["source"],
                    "duration": written.get("duration"),
                })
                continue
            if event_type != "custom.resource.changed" and event_type != "world.changed":
                raise ValueError(
                    f"不支持的事件类型：{event_type!r}（fail-closed："
                    "宁可整批回滚，也不静默丢弃权威变更）"
                )
            if event_type == "world.changed":
                raw_world = state.setdefault("world", {})
                if not isinstance(raw_world, dict):
                    raise ValueError("ruleset_state.world 必须是 {id: 对象} 形式的对象")
                # 首次用到世界状态时用规则声明播种；已存在的对象**不覆盖**，
                # 否则已经解锁的门会在下一批又被声明重新锁上。
                _seed_world(raw_world, mechanics.world_seed())
                descriptors = effect_descriptors_from(event.get("effects"))
                if not descriptors:
                    raise ValueError("world.changed 缺少 effects")
                touched = apply_world_effects(raw_world, descriptors)
                if not touched:
                    continue
                applied_events.append({
                    "type": "world.changed",
                    "actor_id": str(event.get("actor_id") or ""),
                    "objects": touched,
                    "reason": str(event.get("reason") or ""),
                })
                continue
            uid = str(event.get("actor_id") or "")
            if not uid:
                raise ValueError("custom.resource.changed 缺少 actor_id")
            resource_id = str(event.get("resource_id") or "")
            spec = _find_resource(mechanics, resource_id)
            if spec is None:
                raise ValueError(f"未声明的资源：{resource_id!r}")
            try:
                delta = int(event.get("delta", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"资源 {resource_id} 的 delta 不是整数：{event.get('delta')!r}"
                ) from exc
            seat = _ensure_seat(state, instance, mechanics, uid)
            resources = seat["resources"]
            before = int(resources.get(spec.id, spec.default))
            after = before + delta
            cap = custom_state.resource_cap(
                spec,
                custom_state.sheet_attributes(self._sheet(instance, uid)),
                custom_state.sheet_special_stats(self._sheet(instance, uid), {s.id for s in mechanics.resources}),
                resources,
            )
            if cap is not None:
                after = min(after, cap)
            after = max(after, int(event.get("min", 0) or 0))
            resources[spec.id] = after
            applied_events.append({
                "type": "custom.resource.changed",
                "actor_id": uid,
                "resource_id": spec.id,
                "before": before,
                "after": after,
                "reason": str(event.get("reason") or ""),
            })

        for event in deferred_attempts:
            ledger = state.setdefault("attempts", [])
            if not isinstance(ledger, list):
                raise ValueError("ruleset_state.attempts 必须是数组")
            raw_world = state.get("world")
            raw_world = raw_world if isinstance(raw_world, dict) else {}
            target = str(event.get("target") or "")
            entry = record_attempt_entry(
                ledger,
                actor_id=str(event.get("actor_id") or ""),
                outcome=str(event.get("outcome") or ""),
                intent_family=str(event.get("intent_family") or ""),
                target_id=target,
                approach_signature=str(event.get("approach_signature") or ""),
                retry_policy=str(event.get("retry_policy") or RetryPolicy.FREE),
                world_versions=(
                    snapshot_world_versions(raw_world, [target]) if target else {}
                ),
            )
            applied_events.append({
                "type": EVENT_ATTEMPT_RECORD,
                "actor_id": entry["actor_id"],
                "target": target,
                "intent_family": entry["intent_family"],
                "approach_signature": entry["approach_signature"],
                "retry_policy": entry["retry_policy"],
                "outcome": entry["outcome"],
            })

        if not applied_events and not recorded_events:
            return {
                "applied": False,
                "reason": "no-applicable-events",
                "state_version": int(state.get("revision", 0) or 0),
                "events": [],
                "recorded_events": [],
            }

        written = custom_state.write_state(instance, state)
        entry = {
            "batch_id": str(batch.get("batch_id") or ""),
            "intent_type": str(batch.get("intent_type") or ""),
            "events": applied_events,
            # 记录型事件单独一栏：叙事与记忆层靠它读掷骰与成功度。
            "recorded": recorded_events,
        }
        custom_state.append_event(instance, entry)
        return {
            # ``applied`` = 这一批做了事（改了状态**或**留下了裁定记录）。
            "applied": bool(applied_events or recorded_events),
            # ``changed_state`` = 真的改了权威数值。
            "changed_state": bool(applied_events),
            "state_version": int(written.get("revision", 0) or 0),
            "events": applied_events,
            "recorded_events": recorded_events,
        }

    def memory_deltas_from_event_batch(
        self, batch: dict[str, Any], instance: Any,
    ) -> list[dict[str, Any]]:
        """权威批次 → 长期记忆。默认不产出；想记就改这里。

        这是 ``ruleset_gameplay`` 在权威路径里**无条件**调用的方法，
        所以必须存在，不能省。
        """

        del batch, instance
        return []

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------

    def _pre_roll_verdict(
        self, instance: Any, mechanics: CustomMechanics, check: Any, uid: str,
    ) -> tuple[Resolution, list[str]] | None:
        """掷骰**之前**的裁定。返回 ``None`` 表示应该掷骰。

        入参来源分两处：

        - ``can_succeed`` / ``can_fail`` / ``failure_matters`` 来自检定的
          ``adjudication`` 声明 —— 掷骰前还不知道成功度，只能由规则作者表态；
        - "这个办法已经用尽"来自**尝试账本**，但仅在 ``consult_attempts``
          为真时才参与（默认关，因为"试过了还要不要掷"是桌风问题）。
        """

        if not check.can_succeed:
            return (
                Resolution.IMPOSSIBLE,
                ["规则声明：这个办法不可能成功"],
                "adjudication.can_succeed",
            )
        if not check.can_fail:
            return (
                Resolution.AUTO_SUCCESS,
                ["规则声明：这个办法不会失败，不需要掷骰"],
                "adjudication.can_fail",
            )
        if not check.failure_matters:
            return (
                Resolution.AUTO_SUCCESS,
                ["规则声明：失败不留痕迹、可以无限重试 —— 这个骰子没有意义"],
                "adjudication.failure_matters",
            )
        if not check.consult_attempts:
            return None
        for family, approach, target in mechanics.action_keys(check.id):
            status = self.retry_status(
                instance, actor_id=uid, intent_family=family,
                target_id=target, approach_signature=approach,
            )
            if status.get("allowed") is False:
                return (
                    Resolution.AUTO_FAILURE,
                    [f"尝试账本：{status.get('reason')}"],
                    "attempts.retry_policy",
                )
        return None

    def retry_status(
        self, instance: Any, *, actor_id: str, intent_family: str,
        target_id: str = "", approach_signature: str = "",
    ) -> dict[str, Any]:
        """读尝试账本，回答"这个手段还能不能再来一次"。

        这是三问法里 ``failure_matters`` 那一问的**可用切片**。另外两问
        （``can_succeed`` / ``can_fail``）目前还没有声明式来源，仍由世界状态
        与规则文本决定 —— 本条不假装它们已经就绪。

        返回形状：``RetryVerdict`` 的字段 + ``failure_matters``。
        """

        state = getattr(instance, "ruleset_state", None)
        state = state if isinstance(state, dict) else {}
        ledger = state.get("attempts")
        raw_world = state.get("world")
        raw_world = raw_world if isinstance(raw_world, dict) else {}
        target = str(target_id or "")
        verdict = evaluate_retry(
            ledger if isinstance(ledger, list) else [],
            actor_id=str(actor_id or ""),
            intent_family=str(intent_family or ""),
            target_id=target,
            approach_signature=str(approach_signature or ""),
            world_versions=(
                snapshot_world_versions(raw_world, [target]) if target else {}
            ),
        )
        payload = verdict.to_dict()
        payload["failure_matters"] = evaluate_failure_sticks(verdict)
        return payload

    def gameplay_view(
        self, instance: Any, viewer_id: str = "", viewer_is_gm: bool = False,
    ) -> dict[str, Any]:
        return custom_projection.gameplay_view(
            instance, _mechanics_for(instance),
            viewer_id=viewer_id, viewer_is_gm=viewer_is_gm,
        )

    def build_llm_view(self, instance: Any) -> dict[str, Any]:
        ledger = getattr(instance, "event_ledger", None)
        latest = ledger[-1] if isinstance(ledger, list) and ledger else None
        return custom_projection.build_llm_view(
            instance, _mechanics_for(instance), latest_event=latest,
        )

    # ------------------------------------------------------------------
    # 活跃效果：基础值 + 效果 = 有效值
    # ------------------------------------------------------------------

    def active_effects(self, instance: Any, uid: str) -> list[dict[str, Any]]:
        """某个角色挂着的效果（带来源与剩余时长）。

        这是"这个 +5 哪来的、什么时候到期"的答案。没有它，效果就只是
        一个无法追溯的数字漂移。
        """

        state = custom_state.read_state(instance)
        raw_ledger = state.get("effects")
        return effects_for(raw_ledger if isinstance(raw_ledger, dict) else {}, uid)

    def effective_resources(self, instance: Any, uid: str) -> dict[str, int]:
        """席位资源的**有效值**（基础值 + 活跃效果）。基础值本身不变。"""

        return self._effective_resources(instance, uid)

    def expire_effects(self, instance: Any, kind: str) -> list[dict[str, Any]]:
        """时间推进：把到期的效果出列，返回过期的那些。

        基础值从未被修改过，所以"恢复"不需要任何回滚代码 —— 删掉效果，
        有效值自己就回去了。
        """

        state = custom_state.read_state(instance)
        raw_ledger = state.get("effects")
        if not isinstance(raw_ledger, dict) or not raw_ledger:
            return []
        expired = advance_durations(raw_ledger, kind)
        if not expired:
            return []
        state["effects"] = raw_ledger
        custom_state.write_state(instance, state)
        return expired

    # ------------------------------------------------------------------
    # 可选叙事钩子
    # ------------------------------------------------------------------

    def filter_narrative_state_update(
        self, instance: Any, update: dict[str, Any],
    ) -> dict[str, Any]:
        """把规则声明为权威的字段从 LLM 的叙事状态提案里剔除。

        这不是"信任模型会守规矩"，而是让模型**没有能力**改写权威数值：
        ``custom_mechanics.authoritative_fields`` 里列出的键在这里被直接删掉，
        权威值只能经 ``apply_event_batch`` 变化。
        """

        mechanics = _mechanics_for(instance)
        if not mechanics.authoritative_fields:
            return dict(update)
        filtered = deepcopy(update)
        removed = [key for key in mechanics.authoritative_fields if key in filtered]
        for key in removed:
            filtered.pop(key, None)
        if removed:
            logger.info("叙事状态提案已剔除权威字段: %s", ", ".join(removed))
        return filtered

    def on_player_join(self, instance: Any, user_id: str) -> None:
        """新席位加入时：落规则声明快照 + 派发声明式资源。

        **这是规则声明落进 ``ruleset_state`` 的唯一通道。** 协议只在建卡方法里
        把 ``rule`` 交给运行时，而那些方法拿不到 ``instance``；所以声明随角色卡
        走到这里（``character_sheet.ruleset_character.mechanics``，写入点见
        ``src/webui/services/characters.py``），由本方法转存成
        ``ruleset_state["mechanics"]``。游戏期的 ``_mechanics_for`` 就靠它
        读到规则。

        best-effort：宿主在它抛异常时会**删掉席位再抛**，所以这里宁可吞异常。
        """

        uid = str(user_id or "")
        try:
            state = custom_state.read_state(instance)
            touched = False
            declaration = state.get("mechanics")
            if not isinstance(declaration, dict):
                declaration = _declaration_from_sheet(instance, uid)
                if isinstance(declaration, dict):
                    state["mechanics"] = deepcopy(declaration)
                    touched = True
            mechanics = _mechanics_from_declaration(declaration)
            if mechanics is None:
                # 没有角色卡快照时退回宿主传入的规则对象（建卡前/测试夹具）。
                mechanics = _mechanics(CustomDeclarativeRuntime._rule(instance))
            if mechanics.resources:
                _ensure_seat(state, instance, mechanics, uid)
                touched = True
            if touched:
                custom_state.write_state(instance, state)
        except Exception:
            logger.exception("自定义规则状态初始化失败（不阻断加入）: uid=%s", user_id)

    # ------------------------------------------------------------------
    # 迁移
    # ------------------------------------------------------------------

    def migrate_state(self, payload: dict[str, Any], from_version: int) -> dict[str, Any]:
        if from_version == STATE_SCHEMA_VERSION:
            return deepcopy(payload)
        if from_version < STATE_SCHEMA_VERSION:
            # 目前只有 v1；将来加字段时在这里逐版迁移，不要猜。
            raise ValueError(
                f"不支持从 ruleset_state v{from_version} 迁移到 v{STATE_SCHEMA_VERSION}"
            )
        raise ValueError(f"ruleset_state v{from_version} 高于当前支持的 v{STATE_SCHEMA_VERSION}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _rule(instance: Any) -> Any:
        rule = getattr(instance, "rule", None)
        if rule is not None:
            return rule
        # 兼容测试替身：允许把 mechanics 直接挂在 instance.template 上。
        template = getattr(instance, "template", None)
        if isinstance(template, dict):
            return type("_Rule", (), {"template": template, "rule_id": ""})()
        return None

    @staticmethod
    def seed_rule_snapshot(instance: Any, rule: Any) -> bool:
        """把规则声明快照进 ``ruleset_state``，返回是否写入。

        这是游戏期方法看到规则声明的**唯一**通道（协议不把 ``rule`` 传给
        ``available_intents`` / ``resolve_intent`` 等）。拿到 ``rule`` 的入口
        必须至少调用一次：建卡流程、规则切换、测试夹具。
        """

        template = getattr(rule, "template", None)
        if not isinstance(template, dict):
            return False
        declaration = _mechanics_seed(rule)
        if not declaration:
            return False
        state = custom_state.read_state(instance)
        state["mechanics"] = deepcopy(declaration)
        custom_state.write_state(instance, state)
        return True

    @staticmethod
    def _sheet(instance: Any, uid: str) -> Any:
        getter = getattr(instance, "get_character_sheet", None)
        if callable(getter) and uid:
            try:
                return getter(uid)
            except (KeyError, TypeError, ValueError):
                return {}
        return {}

    def _seat_resources(self, instance: Any, uid: str) -> dict[str, int]:
        state = custom_state.read_state(instance)
        seat = (state.get("players") or {}).get(uid)
        resources = seat.get("resources") if isinstance(seat, dict) else None
        return resources if isinstance(resources, dict) else {}

    @staticmethod
    def _consumption_events(
        mechanics: CustomMechanics, check: Any, uid: str, *,
        rolled: bool, succeeded: bool, trace: RuleTrace,
    ) -> tuple[list[dict[str, Any]], list[CostLine], RuleTrace]:
        """按时机产出消耗事件 + 代价账 + 追踪条目。

        不产就不产 —— 该不该扣完全由 ``consumption_charges`` 决定，
        这里不做任何自己的判断（否则扣费点会长出第二套时机语义）。
        """

        events: list[dict[str, Any]] = []
        costs: list[CostLine] = []
        for spec in mechanics.consumptions_for(str(getattr(check, "id", ""))):
            if not consumption_charges(
                spec.timing, rolled=rolled, succeeded=succeeded,
            ):
                reason = f"{spec.timing}：这次裁定不满足扣费条件"
                trace = trace.add(
                    f"consumption:{spec.resource}", "consumption", reason,
                    timing=spec.timing, amount=spec.amount, charged=False,
                )
                continue
            events.append({
                "type": "custom.resource.changed",
                "actor_id": uid,
                "resource_id": spec.resource,
                "delta": -int(spec.amount),
                "reason": f"消耗：{spec.label or spec.resource}",
            })
            label = f"消耗：{spec.label or spec.resource}"
            costs.append(CostLine(
                resource=spec.resource, amount=int(spec.amount),
                timing=spec.timing, reason=label,
            ))
            trace = trace.add(
                f"consumption:{spec.resource}", "consumption",
                f"{spec.timing} 触发，扣 {spec.amount}",
                timing=spec.timing, amount=spec.amount, charged=True,
            )
        return events, costs, trace

    def _unaffordable(
        self, instance: Any, uid: str, consumptions: tuple[Any, ...],
    ) -> tuple[str, int, int] | None:
        """骰前检查：**掷骰之前**就要付的代价够不够。不够则返回三者。

        同一种资源的多笔消耗**先加起来再比较** —— 分开比较会放过
        "两笔各 30、手上只有 50"这类情况。

        ``ON_SUCCESS`` 刻意不在这里检查："失败不消耗"就是它的全部意义，
        骰前拦截等于偷偷把它变成 ``ON_RESOLVE``。
        """

        needed: dict[str, int] = {}
        for spec in consumptions:
            if consumption_checked_before_roll(spec.timing):
                needed[spec.resource] = needed.get(spec.resource, 0) + int(spec.amount)
        if not needed:
            return None
        available = self._effective_resources(instance, uid)
        for resource_id, amount in needed.items():
            have = int(available.get(resource_id, 0) or 0)
            if have < amount:
                return (resource_id, amount, have)
        return None

    def _effective_resources(self, instance: Any, uid: str) -> dict[str, int]:
        """席位资源的**有效值** = 基础值 + 活跃效果。

        基础值本身不动：投影出一份新的字典。所以效果过期时不需要"减回去"，
        那个 +2 自然就没了。
        """

        base = self._seat_resources(instance, uid)
        if not base or not uid:
            return base
        state = custom_state.read_state(instance)
        raw_ledger = state.get("effects")
        active = effects_for(
            raw_ledger if isinstance(raw_ledger, dict) else {}, uid,
        )
        if not active:
            return base
        projected = project_effects({"resources": base}, active)
        return {
            key: int(value)
            for key, value in (projected.get("resources") or {}).items()
            if isinstance(value, int) and not isinstance(value, bool)
        }


def _ensure_seat(
    state: dict[str, Any], instance: Any, mechanics: CustomMechanics, uid: str,
) -> dict[str, Any]:
    """确保席位存在且资源已派生（已有值保留）。"""

    if not uid:
        return {"resources": {}}
    players = state.setdefault("players", {})
    seat = players.get(uid)
    if not isinstance(seat, dict):
        seat = {}
        players[uid] = seat
    existing = seat.get("resources")
    existing = existing if isinstance(existing, dict) else {}
    getter = getattr(instance, "get_character_sheet", None)
    sheet = getter(uid) if callable(getter) else {}
    seat["resources"] = custom_state.derive_resources(
        mechanics, sheet, existing={k: int(v) for k, v in existing.items() if isinstance(v, int)},
    )
    return seat


def _find_check(mechanics: CustomMechanics, check_id: str) -> Any:
    for check in mechanics.checks:
        if check.id == check_id:
            return check
    return None


def _find_resource(mechanics: CustomMechanics, resource_id: str) -> Any:
    for spec in mechanics.resources:
        if spec.id == resource_id:
            return spec
    return None


__all__ = [
    "AUTHORITATIVE_INTENTS",
    "CustomDeclarativeRuntime",
    "INTENT_ADJUST",
    "INTENT_CHECK",
    "MAX_EVENT_LEDGER",
    "rule_binding",
]
