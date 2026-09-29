#!/usr/bin/env python3
"""Stage B（权威意图路径）离线端到端自测。

为什么能离线跑
--------------
``src/webui/services/ruleset_gameplay.py`` 把宿主依赖收敛成一个 9 字段的
``RulesetGameplayDependencies``，其中大部分是纯函数或 registry 方法。把它们自己
组装起来，就能直接调用**生产服务层**的 ``available_actions`` / ``submit_intent``：
测的是真实代码路径，但不需要启动 WebUI、不需要 LLM、不需要 access token。

四道检查
--------
  C0  对照组（Stage A）：``available_actions`` 必须回 ``RULESET_INTENTS_UNAVAILABLE``
  C1  Stage B 打开但存档未绑定 → 必须回 ``RULESET_BINDING_MISMATCH``
  C2  打开且已绑定 → ``available_actions`` 必须给出 ``custom.check.roll`` 等自定义意图
  C3  ``submit_intent(custom.check.roll)`` → 服务端掷骰、EventBatch 落状态、
      ``event_ledger`` 增长、``ruleset_state`` 变化
  C4  连续 20 次 ``will_check``（1d100），统计四档分布（极难/困难/成功/失败）

C0 与 C1-C4 需要不同的**模块级常量**（能力位在导入时求值一次），因此跑在两个
子进程里；父进程只负责调度和汇总。

用法
----
    .venv\\Scripts\\python.exe scripts/dev/test_stage_b.py
    .venv\\Scripts\\python.exe scripts/dev/test_stage_b.py --verbose

退出码 0 = 全部通过。详细说明见 ``docs/STAGE_B_TEST_CN.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_FLAG = "DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS"
RESULT_MARK = "##STAGE_B_RESULT##"

RULE_ID = "custom_freeform"
RULES_DIR = ROOT / "templates" / "rules"

PLAYER = "stageb_player"
GM = "stageb_gm"

VERBOSE = False


def log(message: str) -> None:
    if VERBOSE:
        print(f"    [child] {message}", file=sys.stderr, flush=True)


def _dump(value: Any, limit: int = 600) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + f"...({len(text)} 字符)"


def ensure_import_order() -> None:
    """必须先导入 ``web_server``。

    上游存在一个与本次二开无关的循环导入：若 ``src.rulesets.*`` 或
    ``src.engine.modules.*`` 先被导入，``src/migrations/instance.py`` 会去
    ``src.engine.modules.lorebook_runtime`` 取还没生成的 ``fresh`` 名字而炸掉。
    先导入 ``web_server`` 就不会触发（等价于 ``pytest -p web_server``）。
    """

    if "web_server" not in sys.modules:
        import web_server  # noqa: F401


# ---------------------------------------------------------------------------
# 依赖组装：把宿主能力缩到最小，但调用的是真实服务层
# ---------------------------------------------------------------------------
def build_dependencies(saves_dir: Path):
    """组装 ``RulesetGameplayDependencies``。返回 ``(service, registry, deps)``。"""

    ensure_import_order()

    from src.engine.game_instance import GameRegistry
    from src.rules.loader import RuleBundleLoader
    from src.rules.rule_system import RuleSystem
    from src.rulesets.builtin import build_default_ruleset_registry
    from src.webui.services import ruleset_gameplay as service
    from src.webui.services._common import _parse_game_key

    registry = GameRegistry(saves_dir)

    def load_rule_for_game(instance: Any):
        """等价于 ``WebAPI._load_rule_for_game``，但直接从核心规则目录取。"""

        rule_id = str(getattr(instance, "rule_id", "") or RULE_ID)
        try:
            data = RuleBundleLoader().load_rule(RULES_DIR, rule_id, "")
        except Exception as exc:  # noqa: BLE001 - 诊断用
            log(f"load_rule({rule_id}) 失败: {type(exc).__name__}: {exc}")
            return None
        return RuleSystem(data) if data else None

    deps = service.RulesetGameplayDependencies(
        get_instance=registry.get,
        parse_game_key=_parse_game_key,
        load_rule_for_game=load_rule_for_game,
        ruleset_registry=build_default_ruleset_registry(),
        resolve_adventure_binding=lambda *a, **k: {},
        save_instance=registry.save,
        apply_memory_delta=None,
        resolve_llm_client=lambda: None,
        complete_adventure_node=None,
    )
    return service, registry, deps


def load_rule() -> Any:
    """按宿主的正规路径加载示例规则，得到带 ``template`` 的 ``RuleSystem``。"""

    from src.rules.loader import RuleBundleLoader
    from src.rules.rule_system import RuleSystem

    return RuleSystem(RuleBundleLoader().load_rule(RULES_DIR, RULE_ID, ""))


def build_game(registry: Any, suffix: str, *, bind: bool):
    """造一个"有席位、有规则、可写"的最小对局。

    ``bind=True`` 时同时做两件建卡流程会做的事：
      1. ``bind_ruleset_runtime`` —— 写入规则身份；
      2. ``seed_rule_snapshot`` —— 把规则声明快照进 ``ruleset_state``。
    第 2 步是游戏期方法能拿到规则声明的唯一通道（协议不把 ``rule`` 传给
    ``available_intents`` / ``resolve_intent``）。
    """

    from src.engine.game_instance import GameState
    from src.rulesets.custom.binding import rule_binding
    from src.rulesets.custom.runtime import CustomDeclarativeRuntime

    game_key = ("web", "stageb", suffix)
    instance = registry.get_or_create(game_key)
    instance.world_id = "test_fantasy"
    instance.rule_id = RULE_ID
    instance.gm_uid = GM
    instance.players[PLAYER] = {
        "character_name": "检定者",
        "character_sheet": {
            "race": "人类", "class": "测试者", "level": 1,
            "attributes": {
                "str": 10, "dex": 10, "con": 10,
                "int": 10, "wis": 10, "cha": 10,
            },
            "hp": 10, "max_hp": 10,
            "skills": [], "equipment": [], "inventory": [],
            "deceased": False,
        },
    }
    notes: list[str] = []
    if bind:
        ok = instance.bind_ruleset_runtime(rule_binding())
        notes.append(f"bind_ruleset_runtime -> {ok}")
        if not ok:
            raise RuntimeError("绑定规则运行时失败")
        notes.append(f"ruleset_runtime = {_dump(instance.ruleset_runtime, 200)}")
        seeded = CustomDeclarativeRuntime.seed_rule_snapshot(instance, load_rule())
        notes.append(f"seed_rule_snapshot -> {seeded}")
        if not seeded:
            raise RuntimeError("规则声明快照失败")
    try:
        instance.state = GameState.ACTIVE_ACTION
        notes.append("state -> ACTIVE_ACTION")
    except Exception as exc:  # noqa: BLE001 - 只做记录，失败也继续
        notes.append(f"state 未设置（{type(exc).__name__}），保持 {instance.state!r}")
    return "|".join(game_key), instance, notes


def _case(case_id: str, name: str, passed: bool, detail: str, notes=None) -> dict:
    return {
        "id": case_id,
        "name": name,
        "pass": bool(passed),
        "detail": detail,
        "notes": list(notes or []),
    }


def _safe(case_id: str, name: str, fn) -> dict:
    """跑一个检查；异常变成 FAIL 而不是中断整轮自测。"""

    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - 自测脚本要把失败报出来
        if VERBOSE:
            import traceback

            traceback.print_exc()
        return _case(case_id, name, False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 子进程：Stage A 对照组
# ---------------------------------------------------------------------------
def run_stage_a() -> list[dict]:
    ensure_import_order()

    from src.rulesets.custom import runtime as custom_runtime

    log(f"AUTHORITATIVE_INTENTS = {custom_runtime.AUTHORITATIVE_INTENTS}")
    if custom_runtime.AUTHORITATIVE_INTENTS:
        return [_case("C0", "Stage A 对照组", False, "环境变量未清干净，能力位仍为 True")]

    def check() -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            service, registry, deps = build_dependencies(Path(tmp) / "saves")
            game_key, _instance, notes = build_game(registry, "a0", bind=False)
            result = asyncio.run(
                service.available_actions(deps, game_key, PLAYER, False)
            )
            log(f"available_actions -> {_dump(result)}")
            code = str(result.get("code") or "")
            return _case(
                "C0",
                "Stage A 对照组：意图必须不可用",
                code == "RULESET_INTENTS_UNAVAILABLE",
                f"code={code or '(无错误码)'}  ok={result.get('ok')}",
                notes,
            )

    return [_safe("C0", "Stage A 对照组", check)]


# ---------------------------------------------------------------------------
# 子进程：Stage B 主检查
# ---------------------------------------------------------------------------
def run_stage_b() -> list[dict]:
    ensure_import_order()

    from src.rulesets.custom import runtime as custom_runtime

    log(f"AUTHORITATIVE_INTENTS = {custom_runtime.AUTHORITATIVE_INTENTS}")
    if not custom_runtime.AUTHORITATIVE_INTENTS:
        return [_case("C1", "Stage B 能力位", False, "能力位仍是 False，环境变量没生效")]

    cases: list[dict] = []

    # ---- C1：能力位已开，但存档没绑定 ----
    def check_c1() -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            service, registry, deps = build_dependencies(Path(tmp) / "saves")
            game_key, _instance, notes = build_game(registry, "b1", bind=False)
            result = asyncio.run(
                service.available_actions(deps, game_key, PLAYER, False)
            )
            log(f"C1 available_actions -> {_dump(result)}")
            code = str(result.get("code") or "")
            return _case(
                "C1",
                "已开 Stage B、未绑定 → 拒绝并报 BINDING_MISMATCH",
                code == "RULESET_BINDING_MISMATCH",
                f"code={code or '(无错误码)'}",
                notes,
            )

    cases.append(_safe("C1", "未绑定拒绝", check_c1))

    # ---- C2：绑定后可发现自定义意图 ----
    def check_c2() -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            service, registry, deps = build_dependencies(Path(tmp) / "saves")
            game_key, _instance, notes = build_game(registry, "b2", bind=True)
            result = asyncio.run(
                service.available_actions(deps, game_key, PLAYER, False)
            )
            log(f"C2 available_actions -> {_dump(result)}")
            blob = json.dumps(result, ensure_ascii=False, default=str)
            has_roll = "custom.check.roll" in blob
            return _case(
                "C2",
                "已绑定 → 暴露 custom.check.roll",
                bool(result.get("ok")) and has_roll,
                f"ok={result.get('ok')} 含 custom.check.roll={has_roll}",
                notes + [_dump(result, 400)],
            )

    cases.append(_safe("C2", "意图发现", check_c2))

    # ---- C3 + C4：提交意图、观察落状态与掷骰分布 ----
    def check_c3_c4() -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            service, registry, deps = build_dependencies(Path(tmp) / "saves")
            game_key, instance, notes = build_game(registry, "b3", bind=True)

            before = json.dumps(instance.ruleset_state, ensure_ascii=False, sort_keys=True)
            ledger_before = len(instance.event_ledger)
            result = asyncio.run(
                service.submit_intent(
                    deps, game_key, PLAYER, False,
                    {"type": "custom.check.roll", "check_id": "will_check"},
                )
            )
            log(f"C3 submit_intent -> {_dump(result)}")
            after = json.dumps(instance.ruleset_state, ensure_ascii=False, sort_keys=True)
            ledger_after = len(instance.event_ledger)

            out = [_case(
                "C3",
                "submit_intent → 掷骰 + EventBatch 落状态",
                bool(result.get("ok"))
                and ledger_after > ledger_before
                and after != before,
                f"ok={result.get('ok')}  ledger {ledger_before}→{ledger_after}  "
                f"state_changed={after != before}",
                notes + [f"state: {_dump(json.loads(before), 300)}",
                         f"→ {_dump(json.loads(after), 300)}",
                         f"ledger[-1]: {_dump(instance.event_ledger[-1] if instance.event_ledger else None, 300)}"],
            )]

            # C4：连掷 20 次看分布
            degrees: dict[str, int] = {}
            failures: list[str] = []
            for i in range(20):
                try:
                    res = asyncio.run(
                        service.submit_intent(
                            deps, game_key, PLAYER, False,
                            {"type": "custom.check.roll", "check_id": "will_check"},
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"#{i} {type(exc).__name__}: {exc}")
                    continue
                if not res.get("ok"):
                    failures.append(f"#{i} code={res.get('code')} {res.get('error')}")
                    continue
                batch = res.get("event_batch") or {}
                for degree in _degrees_of(batch) or _degrees_of(res):
                    degrees[degree] = degrees.get(degree, 0) + 1

            out.append(_case(
                "C4",
                "连掷 20 次 will_check（1d100）→ 分布可观察",
                bool(degrees),
                f"成功 {20 - len(failures)}/20  档位={degrees or '(未解析到)'}"
                + (f"  失败样例={failures[:3]}" if failures else ""),
                [f"resource 终值: {_dump(json.loads(json.dumps(instance.ruleset_state, default=str)).get('resources'), 200)}"],
            ))
            return out

    try:
        cases.extend(check_c3_c4())
    except Exception as exc:  # noqa: BLE001
        if VERBOSE:
            import traceback

            traceback.print_exc()
        cases.append(_case("C3", "submit_intent", False, f"{type(exc).__name__}: {exc}"))

    return cases


def _degrees_of(payload: Any) -> list[str]:
    """从任意嵌套结构里收集 ``degree`` 字段。"""

    found: list[str] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            value = node.get("degree")
            if isinstance(value, str) and value:
                found.append(value)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


# ---------------------------------------------------------------------------
# 父进程：调度 + 汇总
# ---------------------------------------------------------------------------
def spawn_child(mode: str) -> tuple[list[dict], str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if mode == "stage-b":
        env[ENV_FLAG] = "1"
    else:
        env.pop(ENV_FLAG, None)
    argv = [sys.executable, str(Path(__file__).resolve()), "--child", mode]
    if VERBOSE:
        argv.append("--verbose")
    proc = subprocess.run(
        argv, cwd=str(ROOT), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    cases: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        if line.startswith(RESULT_MARK):
            try:
                cases = json.loads(line[len(RESULT_MARK):])
            except json.JSONDecodeError:
                pass
    if proc.returncode != 0 or not cases:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return [], "\n".join(detail[-12:]) or f"returncode={proc.returncode}"
    return cases, ""


def main() -> int:
    global VERBOSE

    parser = argparse.ArgumentParser(description="Stage B 离线端到端自测")
    parser.add_argument("--child", choices=("stage-a", "stage-b"), help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="打印每个中间响应")
    args = parser.parse_args()
    VERBOSE = args.verbose

    if args.child:
        cases = run_stage_a() if args.child == "stage-a" else run_stage_b()
        print(RESULT_MARK + json.dumps(cases, ensure_ascii=False), flush=True)
        return 0

    print("Stage B 离线端到端自测")
    print(f"  仓库     : {ROOT}")
    print(f"  规则     : {RULES_DIR / (RULE_ID + '.json')}")
    print(f"  开关     : {ENV_FLAG}")
    print()

    all_cases: list[dict] = []
    for label, mode in (("Stage A 对照组", "stage-a"), ("Stage B 主检查", "stage-b")):
        print(f"--- {label} ---")
        cases, error = spawn_child(mode)
        if error:
            print(f"    子进程失败：\n{error}")
            all_cases.append(_case("C?", label, False, error))
            continue
        for case in cases:
            mark = "PASS" if case["pass"] else "FAIL"
            print(f"    [{mark}] {case['id']}  {case['name']}")
            print(f"           {case['detail']}")
            for note in case.get("notes") or []:
                print(f"           · {note}")
        all_cases.extend(cases)
        print()

    failed = [c for c in all_cases if not c["pass"]]
    print(f"结果：{len(all_cases) - len(failed)}/{len(all_cases)} 通过")
    if failed:
        print("未通过：" + "、".join(c["id"] for c in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
