"""声明式规则的 HTTP 契约：`available-actions` 与 `/intents`。

前端「规则与检定」面板（`frontend-v2/src/features/rulesets/custom/CustomRulesetPanel.vue`）
只调这两个端点，所以这里把它们**当成前端**跑一遍：声明式运行时能不能真的经由
服务层掷骰、落账、并把结果分解与推理链回传。

刻意走服务层而不是直接调 runtime：面板拿到的是 `gameplay` + `available_actions`
+ `result.recorded_events` 这三块，而它们的组装发生在 `ruleset_gameplay` 里。
直接调 runtime 会漏掉"服务层有没有把它们带出来"这一整类问题。
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.engine.game_instance import GameRegistry
from src.rules.loader import RuleBundleLoader
from src.rules.rule_system import RuleSystem
from src.rulesets.adjudication import CONSUMPTION_FAILED
from src.rulesets.contracts import RulesetCapabilities
from src.rulesets.custom.runtime import CustomDeclarativeRuntime
from src.rulesets.legacy_adapter import LegacyRulesetAdapter
from src.rulesets.registry import RulesetRuntimeRegistry
from src.webui.services import ruleset_gameplay
from src.webui.services._common import _parse_game_key

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "templates" / "rules"
RULE_ID = "custom_freeform"
UID = "http_player"
IMPOSSIBLE_COST = 5


class _DeclarativeRuntime(CustomDeclarativeRuntime):
    """打开权威意图开关后的运行时（默认关，见 custom/runtime.py 的注释）。"""

    capabilities = RulesetCapabilities(
        experience_profile="custom",
        character_builder="professional",
        character_lifecycle="rules_aware",
        authoritative_intents=True,
        narrative_turns=True,
    )


class _Shim:
    """最小的 WebAPI 替身：走真实 `ruleset_gameplay` 服务。"""

    def __init__(self, registry: GameRegistry, runtime: _DeclarativeRuntime, template: dict):
        self._reg = registry
        self._rule = RuleSystem({
            "rule_id": RULE_ID,
            "runtime": {"id": runtime.runtime_id, "minimum_version": 1},
            **{"custom_mechanics": template["custom_mechanics"]},
            "attributes": template.get("attributes", []),
        })
        self._dependencies = ruleset_gameplay.RulesetGameplayDependencies(
            get_instance=self._reg.get,
            parse_game_key=_parse_game_key,
            load_rule_for_game=lambda instance: self._rule,
            ruleset_registry=RulesetRuntimeRegistry([LegacyRulesetAdapter(), runtime]),
            resolve_adventure_binding=lambda *a, **k: None,
            save_instance=self._reg.save,
            apply_memory_delta=None,
        )

    async def available_actions(self, game_key: str, requester: str, is_gm: bool = False):
        return await ruleset_gameplay.available_actions(
            self._dependencies, game_key, requester, is_gm,
        )

    async def submit(self, game_key: str, requester: str, body, is_gm: bool = False):
        return await ruleset_gameplay.submit_intent(
            self._dependencies, game_key, requester, is_gm, body,
        )


@pytest.fixture(scope="module")
def template() -> dict:
    return json.loads((RULES_DIR / f"{RULE_ID}.json").read_text(encoding="utf-8"))


@pytest.fixture
def shim(tmp_path, template):
    runtime = _DeclarativeRuntime()
    registry = GameRegistry(tmp_path / "saves")
    instance = registry.get_or_create(("web", "http", "custom"))
    instance.world_id = "test_fantasy"
    instance.rule_id = RULE_ID
    rule = RuleSystem({"rule_id": RULE_ID, "custom_mechanics": template["custom_mechanics"]})
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
    return _Shim(registry, runtime, template), instance


def _intent(check_id: str, **extra):
    return {"type": "custom.check.roll", "check_id": check_id,
            "intent_id": f"intent-{check_id}", **extra}


def _recorded(payload: dict) -> dict:
    events = payload["result"]["recorded_events"]
    return next(item for item in events if item["type"] == "custom.check.resolved")


# ---------------------------------------------------------------------------
# 1. available-actions：面板靠它渲染
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_available_actions_expose_checks_and_the_gameplay_view(shim) -> None:
    api, _instance = shim
    payload = await api.available_actions("web|http|custom", UID)

    assert payload["ok"] is True
    actions = payload["available_actions"]
    checks = {item["check_id"] for item in actions if item["type"] == "custom.check.roll"}
    assert {"will_check", "pick_lock", "break_wall"} <= checks

    gameplay = payload["gameplay"]
    assert gameplay["runtime_id"] == "custom:declarative"
    assert isinstance(gameplay["state_version"], int)
    # 面板按 is_self 找自己的席位；非 GM 看别人只给摘要。
    assert any(seat["is_self"] for seat in gameplay["seats"])


@pytest.mark.asyncio
async def test_only_the_gm_gets_the_resource_adjust_action(shim) -> None:
    """调整资源是越权操作，不应该出现在玩家的动作列表里。"""

    api, instance = shim
    as_player = await api.available_actions("web|http|custom", UID, False)

    def adjusts(payload):
        return [item for item in payload["available_actions"]
                if item["type"] == "custom.resource.adjust"]

    assert adjusts(as_player) == []

    # 自称 GM 不够 —— 服务层会看真身份（GM_IDENTITY_MISSING）。
    forged = await api.available_actions("web|http|custom", UID, True)
    assert forged["ok"] is False
    assert forged["code"] == "GM_IDENTITY_MISSING"

    instance.gm_uid = UID
    as_gm = await api.available_actions("web|http|custom", UID, True)
    assert adjusts(as_gm), "真 GM 必须有调整入口，否则资源卡死时无法救场"


# ---------------------------------------------------------------------------
# 2. /intents：面板提交的就是这条路径
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_submitting_a_check_returns_the_breakdown_and_trace(shim) -> None:
    api, instance = shim
    payload = await api.submit("web|http|custom", UID, _intent("pick_lock"))

    assert payload["ok"] is True
    record = _recorded(payload)
    assert record["degree"] in {"opened", "ajar", "jammed"}
    assert set(record["outcome"]) == {
        "degree", "degree_label", "resolution", "goal", "severity", "costs",
    }
    assert set(record["outcome"]["goal"]) == {"achieved", "extent", "partial"}
    assert [entry["kind"] for entry in record["trace"]][0] == "roll"
    assert record["trace"][0]["rule"] == "check.target"


@pytest.mark.asyncio
async def test_response_carries_the_refreshed_gameplay_view(shim) -> None:
    """面板提交后直接用响应体覆盖本地状态 —— 所以响应必须带新视图。"""

    api, _instance = shim
    before = await api.available_actions("web|http|custom", UID)
    payload = await api.submit("web|http|custom", UID, _intent("will_check"))

    after = payload["gameplay"]
    assert payload["available_actions"], "动作列表也要一起回传"
    assert after["state_version"] >= before["gameplay"]["state_version"]


@pytest.mark.asyncio
async def test_short_circuit_is_visible_to_the_panel(shim) -> None:
    """不掷骰的裁定也要能显示：面板靠 resolution + reason 渲染，没有 outcome。"""

    api, instance = shim
    before = instance.ruleset_state["players"][UID]["resources"]["resolve"]
    payload = await api.submit("web|http|custom", UID, _intent("break_wall"))

    record = _recorded(payload)
    assert record["resolution"] == "IMPOSSIBLE"
    assert record["reason"]
    assert "outcome" not in record, "没掷骰就不该编一个成功度出来"
    # ON_DECLARE：骰子都没掷，代价照付。
    after = instance.ruleset_state["players"][UID]["resources"]["resolve"]
    assert after == before - IMPOSSIBLE_COST


@pytest.mark.asyncio
async def test_unaffordable_intent_comes_back_as_a_code(shim) -> None:
    """扣不起时面板要拿到可判定的错误码，而不是一句自由文本。"""

    api, instance = shim
    resolve = instance.ruleset_state["players"][UID]["resources"]["resolve"]
    for _ in range(resolve // IMPOSSIBLE_COST):
        await api.submit("web|http|custom", UID, _intent("break_wall"))

    payload = await api.submit("web|http|custom", UID, _intent("break_wall"))
    assert payload["ok"] is False
    assert payload["code"] == CONSUMPTION_FAILED


@pytest.mark.asyncio
async def test_effects_reach_the_panel_as_effective_values(shim) -> None:
    """中了效果之后，面板看到的是**有效值**，并且知道底子在哪。"""

    api, instance = shim
    resolve = instance.ruleset_state["players"][UID]["resources"]["resolve"]
    # will_check 的目标就是 resolve，掷 1 = 极难成功 → 不挂效果；这里要的是失败。
    await api.submit("web|http|custom", UID, _intent("pick_lock"))
    gameplay = (await api.available_actions("web|http|custom", UID))["gameplay"]
    seat = next(item for item in gameplay["seats"] if item["is_self"])

    assert seat["resources"]["resolve"]["value"] <= resolve
    # effects 只在真有生效效果时才出现 —— 形状不该平白变胖。
    assert set(seat) <= {"player_id", "name", "is_self", "resources", "effects"}


@pytest.mark.xfail(
    strict=True,
    reason="已知缺口：声明式运行时还没做 intent_id 去重（resolve_intent 里 replayed 恒为 False）。"
           "重放同一意图会再掷一次骰、再扣一次资源。修好后这条会变成 XPASS，"
           "提醒把它换成真断言。",
)
@pytest.mark.asyncio
async def test_replaying_the_same_intent_does_not_charge_twice(shim) -> None:
    """同一个 intent_id 重复提交不应该再扣一次。**目前做不到。**

    用 ``break_wall`` 而不是会掷骰的检定：它声明了 ``can_succeed: false``，必定
    短路到 IMPOSSIBLE，而 ``ON_DECLARE`` 的代价与骰子无关 —— 于是"重放扣了几次"
    是一个**确定性**问题。拿随机骰去测这件事会变成时灵时不灵（两次都失败时不扣费，
    断言就意外成立），那种测试比没有更糟。

    前端的重试、网络重发、用户双击都会走到这条路径，而声明式规则的代价是真扣资源，
    所以这不是抽象的正确性问题。用 xfail 记录，而不是把"重放会重复结算"写成期望。
    """

    api, instance = shim
    intent = _intent("break_wall")
    await api.submit("web|http|custom", UID, intent)
    before_second = instance.ruleset_state["players"][UID]["resources"]["resolve"]

    second = await api.submit("web|http|custom", UID, intent)

    assert second["ok"] is True
    assert second["result"].get("replayed") is True, "重放要能认出来"
    assert instance.ruleset_state["players"][UID]["resources"]["resolve"] == before_second
