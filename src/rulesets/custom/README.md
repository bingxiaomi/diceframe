# 自定义规则运行时（二开指引）

这个包是**你自己的规则大脑**。它的存在是为了回答一个问题：

> 我想自由定规则，但不想改 DiceFrame 引擎。

答案是：实现 `RulesetRuntime` 协议，在 `builtin.py` 注册一行，然后规则模板 JSON 里声明
`"runtime": {"id": "custom:declarative"}`。引擎照旧管多人、回合、存档、私密视图、记忆和 QQ 桥；
**规则机制的一切归你**。

---

## 为什么不用 `check_mechanic`

内置的叙事检定路径把机制压成了 6 个硬编码枚举：

| 字段 | 允许值 |
|---|---|
| `dice_system` | `d20` / `d100` / `none` |
| `check_mechanic.comparison` | `roll_plus_modifier_gte_target` / `roll_lte_target` / `none` |
| `check_mechanic.advantage.type` | `d20_keep_high_low` / `coc_bonus_penalty` / `""` |
| `critical.failure_rule` | 只认 `coc7e` |
| `combat_model` | `hp_based` / `lethal_narrative` / `none` |
| `combat.scheduler.kind` | `round_robin` / `initiative` / `threshold` |

超出这些就要改 `src/engine/checks.py`。本运行时**不用**这套枚举：资源、检定、成功度分级、
效果全部由 `custom_mechanics` 声明，运行时执行声明。加新机制 = 改 JSON，不改引擎。

---

## 目录

```
src/rulesets/custom/
├─ __init__.py      包说明；惰性导出（故意不急切导入 runtime，避免导入环）
├─ manifest.py      custom_mechanics 声明的解析与校验（fail-fast）
├─ dice.py          基于注入 RNG 的掷骰 + 成功度判定（不碰模块级 random）
├─ binding.py       规则绑定四字段常量 + event_ledger 上限
├─ state.py         ruleset_state 读写 + 资源派生 + 目标值求值
├─ projection.py    gameplay_view（前端，含可见性）/ build_llm_view（GM 模型）
└─ runtime.py       RulesetRuntime 协议实现；AUTHORITATIVE_INTENTS 开关
```

注册点：`src/rulesets/builtin.py` —— `build_default_ruleset_registry()` 里的一行。
示例规则：`templates/rules/custom_freeform.json`。

---

## 两个阶段

### Stage A：叙事模式（`AUTHORITATIVE_INTENTS = 0` 时）

不接管回合流水线。玩家照旧自由文本行动，引擎走原有的叙事检定路径。你负责：

- `build_llm_view` —— 把自定义资源状态注入 GM 上下文（你要的"规则状态被 GM 看见"）；
- `filter_narrative_state_update` —— 把 `authoritative_fields` 里的键从 LLM 的状态提案里**删掉**，
  让模型没有能力改写权威数值；
- `on_player_join` —— 新席位加入时派发声明式资源；
- `validate_character` / `derive_character` —— 建卡数值校验与派生。

**零风险。** 以前建议先跑这一档；两档现在都有前端（建卡器 + 检定面板），
所以**默认跑 Stage B**，要退回只需设 `=0`（两个开关必须成对，见
`docs/STAGE_B_TEST_CN.md`）。

### Stage B：权威模式（**默认**）

打开 `src/webui/services/ruleset_gameplay.py` 的权威意图路径：

- `available_intents` —— 前端可点选的动作；
- `resolve_intent` —— 用服务端 `SystemRandom` 掷骰，产出 EventBatch（**不写状态**）；
- `apply_event_batch` —— 唯一的状态写入口，同时写 `event_ledger`。

因为 `narrative_turns=True`，**日常仍是自由文本**；只有你主动把
`ruleset_state["combat"]["status"]` 置为 `"active"` 时才会强制结构化意图（D&D 2024 用的就是这个模式）。

切到 Stage B 还需要前端 host 组件：

```ts
// frontend-v2/src/features/rulesets/registry.ts
const loaders = {
  custom: () => import('./custom/CustomCharacterBuilder.vue'),
}
const playLoaders = {
  'custom:declarative': {
    campaign: () => import('./custom/CustomCampaignPanel.vue'),
  },
}
```

`registry.ts` 是前端唯一的 concrete-ruleset 豁免点（见 `scripts/architecture_fitness.py`），
所以映射必须加在这里，不要在别处 import 你的 feature 目录。

---

## 加一条新机制（3 步）

1. **改规则 JSON**（`data/templates/rules/你的规则.json`，或直接改本模板）：

```json
"custom_mechanics": {
  "resources": [
    {"id": "sanity", "name": "理智", "initial": {"attribute": "pow"}, "max": {"constant": 99}}
  ],
  "checks": [
    {"id": "sanity_check", "name": "理智检定", "dice": "1d100", "comparison": "lte",
     "target": {"resource": "sanity"},
     "degrees": [
       {"id": "extreme", "label": "极难成功", "max_ratio": 0.2},
       {"id": "hard", "label": "困难成功", "max_ratio": 0.5},
       {"id": "success", "label": "成功", "max_ratio": 1.0},
       {"id": "failure", "label": "失败", "fallback": true}
     ]}
  ],
  "effects": [
    {"check": "sanity_check", "degree": "failure", "resource": "sanity", "delta": "-1d6"}
  ],
  "authoritative_fields": ["resources", "attributes", "skills"]
}
```

2. **重启 / 重载**。声明在规则加载时解析，非法声明会在日志里报错并按空声明运行（不静默改数值）。

3. **如果现有声明表达不了**：那说明你要的是新机制种类（骰池、卡牌、多轮累积……），
   到 `manifest.py` 加声明类型 + `runtime.py` 加执行分支。**这是唯一需要写代码的地方，
   而它只属于你自己，不影响引擎。**

取值引用支持 `{"attribute": k}` / `{"special_stat": k}` / `{"resource": k}` / `{"constant": n}`，
也接受 `"pow"` 这种简写（等价于 `{"attribute": "pow"}`）。

---

## 工程纪律

1. **不要在本包里 import `src.webui` / `src.compat` / `src.web_transport`** ——
   `tests/architecture/test_dependencies.py` 用 AST 守卫会失败。
2. **不要用 `from src.engine.modules import ...`** —— 这个包的 `__init__` 会拉入 lorebook 存储并形成导入环。
   直接读写 `instance.ruleset_state` / `instance.event_ledger` 属性（D&D 2024 也是这么做的）。
3. **不要给 JSON 加可执行代码字段**。`src/rulesets/bundle.py` 的 `FORBIDDEN_EXECUTION_KEYS`
   有意拒绝 `python`/`javascript`/`script`/`code`/`eval`/`module`/`callable`。想加机制就加声明类型 + 代码分支，
   别开代码执行的口子。
4. **改 `STATE_SCHEMA_VERSION` 必须同时在 `migrate_state` 加迁移分支**。这是持久化契约，老存档要能读。
5. **`memory_deltas_from_event_batch` 必须存在** —— 权威路径无条件调用它，删了会 AttributeError。
6. **LLM 不是 authority**。叙事可以描述结果，但数值只能经 `apply_event_batch` 变化。

---

## 已验证行为

在 Python 3.14 + 本仓库依赖下跑通的端到端流程：

```
规则加载 → runtime 绑定解析 → 声明解析
on_player_join        → 派发资源 {resolve: 50}
available_intents     → Stage A 返回 []；Stage B 返回检定/调整动作
validate_intent       → 拒绝未知检定 / 未知意图类型
resolve_intent        → d100 lte 检定 + 4 档成功度，失败触发 -1d6 效果
apply_event_batch     → 50 → 49，revision 自增，event_ledger 落 1 条
gameplay_view         → GM/本人看到数值；其他玩家只见 resource_count（隐私边界）
filter_narrative_state_update → 剔除了 resources/attributes/skills
```

---

## 已知限制

- `character_lifecycle="legacy"`：本运行时暂不接管角色卡权威状态，所以**不会**写入
  `ruleset_character` / `rule_binding`。要让存档绑定你的 runtime（`bind_ruleset_runtime`），
  需要改成 `rules_aware` 并在 `normalize_character_submission` 里返回
  `{"ruleset_character": {...}, "rule_binding": {...}}`（范本：`src/rulesets/dnd2024/character/builder.py`）。
- 前端组件已提供：`frontend-v2/src/features/rulesets/custom/` 下是建卡器与
  「规则与检定」面板（在 `registry.ts` 里注册 `profile="custom"` 与 `runtime="custom:declarative"`）。
  面板目前把所有规则声明都渲染给所有玩家看 —— 包括给规则作者看的推理链；
  按角色区分是待办（见 `docs/`）。
- 推理链（`RuleTrace`）与状态版本号目前**不过滤观众**：它们在玩家面板上也能看到。
- 资源上限用 `max` 声明；`delta` 的下限目前固定为 0（按需在 `apply_event_batch` 里改成声明式）。
