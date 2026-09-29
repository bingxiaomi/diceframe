"""``RulesetRuntime`` 协议实现：自定义声明式规则。

这个类就是"你自己的规则大脑"。引擎只通过这些方法调用你：

    建卡   describe_experience / validate_character / derive_character
           / finalize_character / normalize_character_submission
    结算   available_intents / prepare_intent_submission / validate_intent
           / resolve_intent / apply_event_batch
    投影   gameplay_view（前端）/ build_llm_view（GM 模型）
    生命周期 migrate_state / project_legacy_character

两类运行模式（``AUTHORITATIVE_INTENTS`` 开关）：

**Stage A（默认，``authoritative_intents=False``）**
    完全不接管回合流水线。玩家照常自由文本行动，引擎走
    ``check_mechanic`` + ``engine/checks.py`` 的叙事检定路径。本运行时负责：

    - 把自定义资源状态注入 GM 上下文（``build_llm_view``）；
    - 阻止 LLM 通过叙事标签改写权威字段（``filter_narrative_state_update``）；
    - 新席位加入时派发初始资源（``on_player_join``）。

**Stage B（``authoritative_intents=True``）**
    打开权威意图路径：``available_intents`` 暴露可点选动作，
    ``resolve_intent`` 用服务端 RNG 掷骰并产出 EventBatch，``apply_event_batch``
    落状态。因为 ``narrative_turns=True``，**日常仍是自由文本**，只有你主动把
    ``ruleset_state["combat"]["status"]`` 置为 ``"active"`` 才会强制结构化意图
    （D&D 2024 用的就是这个模式）。

切到 Stage B 需要同步做前端 host 组件（见 README）。
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from functools import lru_cache
from typing import Any
from uuid import uuid4

from src.rulesets.contracts import RulesetCapabilities
from src.rulesets.custom import binding as custom_binding
from src.rulesets.custom import projection as custom_projection
from src.rulesets.custom import state as custom_state
from src.rulesets.custom.binding import (
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
# 模式开关：Stage B（权威意图路径）。默认关闭 = Stage A。
#
# 不用改源码：设置环境变量即可，但**必须在服务启动之前**设置，
# 因为能力位在模块导入时求值一次。
#
#     PowerShell:  $env:DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS = "1"
#     bash:        export DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS=1
#
# 打开后还需要另外两个条件，否则 HTTP 接口会明确报错：
#   - 存档必须已绑定本运行时，否则 ``RULESET_BINDING_MISMATCH``；
#   - 前端 host 组件要能渲染自定义意图，否则点了没用。
# 自测：``scripts/dev/test_stage_b.py``（离线，不需要服务/LLM/token）。
# 说明：``docs/STAGE_B_TEST_CN.md``。
# ---------------------------------------------------------------------------
_TRUTHY = frozenset({"1", "true", "yes", "on"})

AUTHORITATIVE_INTENTS = (
    os.environ.get("DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS", "").strip().lower()
    in _TRUTHY
)

INTENT_CHECK = "custom.check.roll"
INTENT_ADJUST = "custom.resource.adjust"


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


def _mechanics_from_state(instance: Any) -> CustomMechanics | None:
    """优先读存档内的规则声明快照。

    ``RulesetRuntime`` 协议只在**建卡**方法（``describe_experience`` /
    ``builder_choices`` / ``validate_character`` / ``derive_character`` /
    ``finalize_character`` / ``normalize_character_submission``）里把 ``rule``
    交给运行时；游戏期方法（``available_intents`` / ``resolve_intent`` /
    ``apply_event_batch`` / ``gameplay_view`` / ``build_llm_view``）只拿得到
    ``instance``。所以声明必须在建卡时经 ``seed_rule_snapshot`` 快照进
    ``ruleset_state``，游戏期从存档读。

    这也让存档自包含：规则文件之后被改动，不会让进行中的对局惄惄换规则
    （要换规则就递增 ``CONTENT_VERSION`` 并走迁移）。
    """

    state = getattr(instance, "ruleset_state", None)
    if not isinstance(state, dict):
        return None
    declaration = state.get("mechanics")
    if declaration is None:
        return None
    try:
        return _mechanics_from_json(
            json.dumps(declaration, sort_keys=True, ensure_ascii=False)
        )
    except (TypeError, ValueError) as exc:
        logger.error("ruleset_state.mechanics 快照非法，回退规则对象: %s", exc)
        return None


def _mechanics_for(instance: Any) -> CustomMechanics:
    """游戏期取 mechanics：存档快照优先，其次宿主传入的规则对象。"""

    return _mechanics_from_state(instance) or _mechanics(
        CustomDeclarativeRuntime._rule(instance)
    )


class CustomDeclarativeRuntime:
    """自定义声明式规则的运行时实现。"""

    runtime_id = RUNTIME_ID
    runtime_version = RUNTIME_VERSION
    state_schema_version = STATE_SCHEMA_VERSION
    capabilities = RulesetCapabilities(
        experience_profile="custom",
        character_builder="guided",
        character_lifecycle="legacy",
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
            "builder_mode": "guided",
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
        del rule, draft
        # character_builder 不是 "professional"，引擎不会走这个入口；
        # 刻意与 legacy 保持一致地显式失败，避免被误当成可选建卡来源。
        raise NotImplementedError("custom rules use the existing guided character schema")

    def validate_character(self, rule: Any, draft: dict[str, Any]) -> list[str]:
        validator = getattr(rule, "validate_character", None)
        if not callable(validator):
            return []
        return list(validator(draft))

    def derive_character(self, rule: Any, draft: dict[str, Any]) -> dict[str, Any]:
        errors = self.validate_character(rule, draft)
        if errors:
            raise ValueError("; ".join(errors))
        return deepcopy(draft)

    def finalize_character(self, rule: Any, draft: dict[str, Any]) -> dict[str, Any]:
        return self.derive_character(rule, draft)

    def normalize_character_submission(
        self, rule: Any, character: dict[str, Any], locale: str = "",
    ) -> dict[str, Any]:
        del rule, locale
        return deepcopy(character)

    def project_legacy_character(self, character: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(character)

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
            resources = self._seat_resources(instance, uid)
            target = custom_state.resolve_target(
                mechanics, self._sheet(instance, uid), ref=check.target, resources=resources,
            )
            outcome = resolve_check(rng, check, target=target)
            events.append({
                "type": "custom.check.resolved",
                "actor_id": uid,
                **outcome.to_dict(),
            })
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
        """把 EventBatch 落到 ``ruleset_state``。唯一的状态写入口。"""

        events = batch.get("events")
        if not isinstance(events, list):
            raise ValueError("event batch 缺少 events 数组")
        state = custom_state.read_state(instance)
        mechanics = _mechanics_for(instance)
        applied_events: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if str(event.get("type") or "") != "custom.resource.changed":
                continue
            uid = str(event.get("actor_id") or "")
            spec = _find_resource(mechanics, str(event.get("resource_id") or ""))
            if not uid or spec is None:
                continue
            try:
                delta = int(event.get("delta", 0) or 0)
            except (TypeError, ValueError):
                continue
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

        if not applied_events:
            return {"applied": False, "reason": "no-applicable-events", "state_version": int(state.get("revision", 0) or 0)}

        written = custom_state.write_state(instance, state)
        entry = {
            "batch_id": str(batch.get("batch_id") or ""),
            "intent_type": str(batch.get("intent_type") or ""),
            "events": applied_events,
        }
        custom_state.append_event(instance, entry)
        return {
            "applied": True,
            "state_version": int(written.get("revision", 0) or 0),
            "events": applied_events,
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
        """新席位加入时派发声明式资源。best-effort，失败不阻断加入。"""

        try:
            mechanics = _mechanics_for(instance)
            if not mechanics.resources:
                return
            state = custom_state.read_state(instance)
            _ensure_seat(state, instance, mechanics, str(user_id or ""))
            custom_state.write_state(instance, state)
        except Exception:
            logger.exception("自定义规则资源派发失败（不阻断加入）: uid=%s", user_id)

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
        declaration = template.get("custom_mechanics")
        if declaration is None:
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
