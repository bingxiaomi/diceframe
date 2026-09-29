# Stage B（权威意图路径）测试指南

> 面向本仓库的二开分支。Stage A / Stage B 的来源见 `src/rulesets/custom/runtime.py`
> 的模块 docstring 与 `docs/PROGRESS_CN.md`。

## 1. 一句话区分

| | Stage A（默认） | Stage B |
| --- | --- | --- |
| 回合流程 | 完全走引擎原有自由文本 + `check_mechanic` | 增加"服务端权威意图"通道 |
| 运行时职责 | 注入资源到 GM 上下文、拦住 LLM 改写权威字段、席位加入派发资源 | 再额外：`available_intents` / `validate_intent` / `resolve_intent` / `apply_event_batch` |
| 日常玩法 | 自由文本 | **仍是自由文本**（`narrative_turns=True`）；只有把 `ruleset_state["combat"]["status"]` 置为 `"active"` 才强制结构化意图 |
| 前端要求 | 无 | 需要能渲染自定义意图的 host 组件 |

Stage A 的价值是**零风险**：不接管任何流水线，只做投影和护栏。

## 2. 开关（**默认已开**）

两个开关**默认就是开的**，所以正常启动（`web_ui.bat` / `scripts/start_webui.py`）
不需要设任何环境变量。

能力位在模块导入时求值一次，所以**要改就必须在启动服务之前**设置：

```powershell
# PowerShell —— 退回 Stage A（只做投影与护栏，不接管回合）
$env:DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS = "0"
$env:DICEFRAME_CUSTOM_PROFESSIONAL_BUILDER = "0"
```

```bash
# bash
export DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS=0
export DICEFRAME_CUSTOM_PROFESSIONAL_BUILDER=0
```

**两个开关必须成对**：绑定只在 `character_builder == "professional"` 的分支里写入，
所以只关掉 `PROFESSIONAL_BUILDER` 而留着 `AUTHORITATIVE_INTENTS` 会得到
`RULESET_BINDING_MISMATCH`（有权威意图，但存档没被绑定）。

> **为什么曾经默认关，现在翻转了**：打开后 `describe_experience` 返回
> `profile="custom"`，而前端注册表当时没有对应组件，入局 / 建房页会报
> `Unsupported ruleset experience`；也没有能提交自定义意图的面板。
> 两者现已落地（`frontend-v2/src/features/rulesets/custom/`），所以默认值翻转。

## 3. 三道门槛（缺一不可）

`_context()` 在放行一个意图请求前会依次检查，每一道都有明确的错误码：

| 门槛 | 检查什么 | 不满足时的错误码 | HTTP |
| --- | --- | --- | --- |
| ① 能力位 | `runtime.capabilities.authoritative_intents` | `RULESET_INTENTS_UNAVAILABLE` | 409 |
| ② 绑定 | `instance.ruleset_runtime["id"] == runtime.runtime_id` | `RULESET_BINDING_MISMATCH` | 409 |
| ③ 规则快照 | `ruleset_state["mechanics"]` 存在 | 不会报错，但表现为 `declared_checks: []`、意图为空、提交报 `UNKNOWN_CHECK` | — |

**门槛②为什么容易踩**：绑定只由 `instance.bind_ruleset_runtime(...)` 写入，而全仓库只有两处调用
（`src/webui/services/characters.py` 与 `src/webui/services/ruleset_characters.py`），
都在 `character_builder == "professional"` / `character_lifecycle == "rules_aware"` 的建卡流程里。

> **已解决（阶段 0，现为默认）**：专业建卡分支会走，绑定与规则快照**自动**发生，
> 不再需要手工改存档。设 `DICEFRAME_CUSTOM_PROFESSIONAL_BUILDER=0` 会退回
> `guided` / `legacy`，那时普通对局不会被绑定。

**门槛③为什么会存在**：`RulesetRuntime` 协议只在**建卡方法**里把 `rule` 交给运行时；
游戏期方法（`available_intents` / `validate_intent` / `resolve_intent` /
`apply_event_batch` / `gameplay_view` / `build_llm_view`）只拿得到 `instance`，
而且没有任何建卡方法同时拿得到 `instance`。

解法是让规则声明**随角色卡走**，再由唯一一个同时拿得到 `instance` 的钩子落盘：

```
finalize_character(rule, draft)                   有 rule、无 instance
  └→ {"rule_binding":…, "ruleset_character": {…, "mechanics": 声明}}
       ↓  characters.py 组装 character_sheet
     cs["ruleset_character"] = character["ruleset_character"]
       ↓  characters.py 调 on_player_join()
     ruleset_state["mechanics"] = sheet["ruleset_character"]["mechanics"]
```

游戏期读规则时按可信度降序回退（`runtime._mechanics_for`）：
`ruleset_state` 快照 → 席位角色卡（**只读**自愈，老存档用）→ 宿主传入的规则对象。
读路径**不写**状态 —— `available_intents` / `gameplay_view` 可能在写锁之外被调用。

## 4. 离线自测（推荐先跑这个）

```powershell
cd <仓库>
$env:PYTHONIOENCODING = "utf-8"
& "c:\Users\user\trpg\.venv\Scripts\python.exe" "scripts\dev\test_stage_b.py"
```

它**不需要启动服务、不需要 LLM、不需要 access token**：直接组装
`RulesetGameplayDependencies` 并调用生产服务层 `src/webui/services/ruleset_gameplay.py`
的 `available_actions` / `submit_intent`，所以测的是真实代码路径。

五道检查：

| ID | 检查 | 期望 |
| --- | --- | --- |
| C0 | Stage A 对照组 | `available_actions` 回 `RULESET_INTENTS_UNAVAILABLE` |
| C1 | 已开 Stage B、未绑定 | 回 `RULESET_BINDING_MISMATCH` |
| C2 | 已绑定 + 已快照 | `available_actions` 给出 `custom.check.roll` |
| C3 | 提交一次 `will_check` | `ok=True`、`event_ledger` 增长、`ruleset_state` 变化 |
| C4 | 连掷 20 次 | 能观察到 1d100 的四档分布 |

C0 与 C1–C4 需要不同的模块级常量，所以脚本会自己起两个子进程；父进程只做调度汇总。

实测结果（本仓库，`templates/rules/custom_freeform.json`）：

```
--- Stage A 对照组 ---
    [PASS] C0  code=RULESET_INTENTS_UNAVAILABLE  ok=False
--- Stage B 主检查 ---
    [PASS] C1  code=RULESET_BINDING_MISMATCH
    [PASS] C2  ok=True 含 custom.check.roll=True
           available_actions = [{"check_id":"will_check","dice":"1d100",
                                 "label":"意志检定","type":"custom.check.roll"}]
    [PASS] C3  ok=True  ledger 0→1  state_changed=True
           ledger[-1] = {"events":[{"actor_id":"stageb_player","after":47,"before":50,
                                    "reason":"意志检定:失败","resource_id":"resolve",
                                    "type":"custom.resource.changed"}],
                         "intent_type":"custom.check.roll"}
    [PASS] C4  成功 20/20  档位={'failure':16,'extreme':1,'hard':1,'success':2}

结果：5/5 通过
```

C3 的 `before:50 → after:47` 就是"服务端掷骰 → 产出 EventBatch → 落状态"的完整证据；
C4 的 16/20 失败率来自示例规则里 `will_check` 的 `lte` 阈值较苛刻（约 20% 成功率）。

加 `--verbose` 会打印每一步的完整响应和子进程堆栈。

## 5. 在真实对局 / WebUI 里验证

离线自测覆盖了服务层，但没覆盖 HTTP 路由和鉴权。要走真实链路：

1. 在 WebUI 里用 `custom_freeform` 规则开一局（这样才有存档）。
2. **停掉服务**，给该存档补上绑定和规则快照（因为 `guided`/`legacy` 建卡流程不会写）：

   > 如果已经打开 `DICEFRAME_CUSTOM_PROFESSIONAL_BUILDER=1` 走专业建卡
   > （阶段 0），这一步就**不需要**了 —— 绑定与快照会在建卡时自动写入。
   > 但那个开关目前要求前端先有 `profile="custom"` 的建卡组件，所以现阶段
   > 手工补一次仍然是最快的验证路径。

```python
import sys
sys.path.insert(0, r"<仓库绝对路径>")
import web_server  # 必须先导入，规避上游 lorebook 循环导入
from src.engine.game_instance import GameRegistry
from src.rules.loader import RuleBundleLoader
from src.rules.rule_system import RuleSystem
from src.rulesets.custom.binding import rule_binding
from src.rulesets.custom.runtime import CustomDeclarativeRuntime

saves = r"<仓库绝对路径>\data\saves"
registry = GameRegistry(__import__("pathlib").Path(saves))
instance = registry.get(("web", "房间", "对局"))          # 改成真实 game_key
assert instance, "没找到对局"

print("绑定  :", instance.bind_ruleset_runtime(rule_binding()))
rule = RuleSystem(RuleBundleLoader().load_rule(
    r"<仓库绝对路径>\templates\rules", "custom_freeform", ""))
print("快照  :", CustomDeclarativeRuntime.seed_rule_snapshot(instance, rule))

import asyncio
asyncio.run(registry.save(instance))
```

3. **重启服务**（环境变量带上 `DICEFRAME_CUSTOM_AUTHORITATIVE_INTENTS=1`）。
4. 用 `data/access_token.txt` 里的口令做 Bearer 调接口。
   `game_key` 在 URL 里是 `平台|房间|对局`（`|` 分隔）：

```bash
TOKEN=$(cat data/access_token.txt)

# 看可用动作：Stage A 时这里是 []，Stage B 应出现 custom.check.roll
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:<端口>/api/games/web%7C%E6%88%BF%E9%97%B4%7C%E5%AF%B9%E5%B1%80/available-actions"

# 提交意图
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"type":"custom.check.roll","check_id":"will_check"}' \
  "http://127.0.0.1:<端口>/api/games/web%7C%E6%88%BF%E9%97%B4%7C%E5%AF%B9%E5%B1%80/intents"
```

状态码映射（`_ruleset_gameplay_status`）：`409` = 能力位关闭或未绑定或回合处理中；
`403` = 鉴权/席位/GM 门控；`404` = 对局或规则不存在；`503` = LLM 未配置；
`422` = 其余业务错误。

## 6. 还没做完的部分

Stage B 要达到"能玩"还差两块，都不在本测试覆盖范围内：

1. **前端 host 组件** —— `available_actions` 返回的意图需要能渲染成可点按钮，
   并把点击结果落到 `ruleset_state` 的新值上。目前前端只认引擎自己的动作卡片。
2. **`custom` 的建卡组件**（阶段 0b）—— 后端专业建卡已经就绪（默认关），
   但 `profile="custom"` 还没有对应的 Vue 组件，所以还不能把默认值打开。
   参考实现：`frontend-v2/src/features/rulesets/dnd2024/create/Dnd2024CharacterBuilder.vue`。

`character_lifecycle="rules_aware"` 以及"绑定 + 规则快照自动产生"这条链路**已经实现**
（阶段 0，默认关闭；打开方式见第 2 节的 `DICEFRAME_CUSTOM_PROFESSIONAL_BUILDER`）。

除此之外，Stage A 是稳定可用的形态：规则数值、资源和叙事护栏都已经生效，
只是检定仍由引擎的叙事检定路径走。

## 6.5 阶段 0 的验收测试

`tests/rulesets/test_custom_character_lifecycle.py`（17 项）覆盖了建卡方法与那两道门槛。
关键写法是**继承真 runtime、只覆盖 `capabilities`**，这样默认能力位保持关闭也能测专业分支：

```python
class _ProfessionalRuntime(CustomDeclarativeRuntime):
    capabilities = RulesetCapabilities(
        character_builder="professional", character_lifecycle="rules_aware",
        authoritative_intents=True, narrative_turns=True,
    )
```

三条最值得看的断言：

| 测试 | 保证什么 |
| --- | --- |
| `test_quick_presets_are_directly_finalizable` | 预设 draft 能原样喂给 finalize（前端一键建卡靠这条） |
| `test_normalize_discards_client_derived_values` | 客户端提交的 hp/AC 被丢弃重算 |
| `test_join_binds_runtime_and_seeds_declaration` | **阶段 0 的验收**：绑定 + 规则快照自动产生 |

## 7. 相关文件

| 文件 | 作用 |
| --- | --- |
| `src/rulesets/custom/runtime.py` | 运行时实现；两个 env 开关、6 个建卡方法、`seed_rule_snapshot` |
| `src/rulesets/custom/manifest.py` | 解析 `custom_mechanics`（无枚举白名单，fail-fast） |
| `src/rulesets/custom/state.py` | 读写 `instance.ruleset_state` / `event_ledger` |
| `src/rulesets/custom/binding.py` | `rule_binding()` 四字段 + 版本常量 |
| `src/webui/services/ruleset_gameplay.py` | HTTP 与服务层门控（`_context`） |
| `scripts/dev/test_stage_b.py` | 本指南第 4 节的离线自测 |
| `tests/rulesets/test_custom_character_lifecycle.py` | 阶段 0 验收（建卡方法 + 绑定 + 规则快照） |
| `templates/rules/custom_freeform.json` | 示例规则（`will_check` / `resolve`） |
