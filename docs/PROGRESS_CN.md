# 二开进度说明

> 快照日期：2026-09-29
> 基线：上游 `main` @ `e4f01c42`（DiceFrame v2.6.1）
> 部署与运行方法见 `docs/DEPLOY_CN.md`

---

## 1. 目标与路线（已确定）

| 项 | 决定 |
|---|---|
| 用途 | 和朋友跑团，**非商业化**，可开源 |
| 路线 | **二开 DiceFrame**（不重写引擎，只在官方扩展点接自己的规则系统） |
| 二开重点 | **规则可设定 / 可导入规则书 / 框架内自由调整** |
| 技术切入 | 实现 `RulesetRuntime` 协议 → 规则机制完全归自己，**不修改引擎** |

### 为什么选"自写 Runtime"而不是改引擎

上游把规则压成了 6 个硬编码枚举，超出就要改 `src/engine/checks.py`：

| 字段 | 内置允许值 |
|---|---|
| `dice_system` | `d20` / `d100` / `none` |
| `check_mechanic.comparison` | `roll_plus_modifier_gte_target` / `roll_lte_target` / `none` |
| `check_mechanic.advantage.type` | `d20_keep_high_low` / `coc_bonus_penalty` / `""` |
| `critical.failure_rule` | 只认 `coc7e` |
| `combat_model` | `hp_based` / `lethal_narrative` / `none` |
| `combat.scheduler.kind` | `round_robin` / `initiative` / `threshold` |

而 `src/rulesets/contracts.py` 的 `RulesetRuntime` 协议是**官方留给第三方规则实现的插槽**（`registry.py` +
`builtin.py` 注册）。走这条路：规则机制零天花板，引擎一行不改，多人/存档/隐私/记忆/QQ 桥全部白拿。

---

## 2. 已完成

### 2.1 自定义规则运行时（`src/rulesets/custom/`）

| 文件 | 职责 |
|---|---|
| `manifest.py` | 解析规则 JSON 的 `custom_mechanics`：资源 / 检定 / 成功度分级 / 效果 / 权威字段。**无枚举白名单**，非法声明 fail-fast |
| `dice.py` | 掷骰与成功度判定；**必须使用调用方注入的 `SystemRandom`**，不碰模块级 `random` |
| `binding.py` | `runtime_id` / `content_version` / `state_schema_version` 常量 |
| `state.py` | 权威状态读写（直接操作 `instance.ruleset_state` / `event_ledger`）、资源派生、目标值求值 |
| `projection.py` | `gameplay_view`（含**玩家可见性边界**）/ `build_llm_view`（GM 上下文） |
| `runtime.py` | `RulesetRuntime` 协议实现 + `AUTHORITATIVE_INTENTS` 模式开关 |
| `README.md` | 二开指引：怎么加机制、工程纪律、已知限制 |

注册点：`src/rulesets/builtin.py` 加一行 `CustomDeclarativeRuntime()`。
示例规则：`templates/rules/custom_freeform.json`（声明 `"runtime": {"id": "custom:declarative"}`）。

### 2.2 两阶段模式（关键设计）

用一个开关分两档推进，**不用一次赌完**：

| | Stage A（默认，`authoritative_intents=False`） | Stage B（改为 True） |
|---|---|---|
| 回合流水线 | 不接管，玩家照常自由文本 | 打开权威意图路径 |
| 运行时负责 | `build_llm_view` 注入规则状态给 GM；`filter_narrative_state_update` 阻止 LLM 改写权威数值；`on_player_join` 派发初始资源；建卡校验 | 加 `available_intents` / `resolve_intent` / `apply_event_batch` |
| 前端 | 不需要 | 需补 host 组件（`registry.ts` 加映射） |
| 风险 | 零 | 中 |

Stage B 复用了 D&D 2024 的模式：`requires_structured_intent = authoritative_intents and (not narrative_turns or combat_active)`
→ 日常仍是自由叙事，只有规则主动把 `ruleset_state["combat"]["status"]` 置为 `active` 时才强制结构化结算。

### 2.3 已验证（端到端实跑，非推测）

在 Python 3.14 + 仓库依赖下执行通过：

```
registered: ('core:dnd2024', 'core:legacy', 'custom:declarative')     ← 运行时注册成功
rule: custom_freeform | runtime binding: {'id': 'custom:declarative', 'minimum_version': 1}

检定分布（d100 lte, target=50）
  roll=42 → success     roll=20 → hard      roll=7 → extreme     roll=51 → failure

生命周期
  on_player_join  → 派发资源 {'resolve': 50}
  validate_intent → 拒绝未知检定 / 未知意图类型（fail-closed）
  resolve_intent  → 4 档成功度 + 失败触发 -1d6 效果（使用注入的 SystemRandom）
  apply_event_batch → 50 → 49，revision 自增，event_ledger 追加

隐私边界
  本人/GM 看到数值；其他玩家只见 resource_count             ← 可见性生效

叙事守卫
  filter_narrative_state_update 剔除 resources/attributes/skills
```

其他检查：**Pylance 对 8 个新文件零报错**；架构守卫合规（`src/rulesets` 不在 `_BACKEND_GENERIC_DIRECTORIES`
扫描范围；concrete 规则只匹配 `src.rulesets.dnd2024`；本包不依赖 `webui` / `compat` / `web_transport`）。

### 2.4 服务可运行 + 部署文档

- 前端构建成功（`registry.npmmirror.com` 镜像，install + build 共 **56 秒**）
- 服务启动成功：`GET /` = 200、`/v2-assets/*` = 200、`/api/rules` = 401（鉴权正常）
- 规则同步：`templates/rules/` → `data/templates/rules/`，`preserved: 1`（自定义规则被保留）
- 新增 `scripts/dev/build_frontend.py`（可观察构建：实时日志 + 心跳状态 + 镜像 + 跳过 Playwright 下载）
- 新增 `scripts/dev/watch_build.py`（构建观察器：静默时长 + 进程存活 → 区分"在下载"与"真卡死"）
- 新增 `docs/DEPLOY_CN.md`（部署 / 联机安全 / 排错，含 17 条排错项）

---

## 3. 未完成 / 已知限制

| 项 | 状态 | 说明 |
|---|---|---|
| **规则书导入管线** | ❌ 未做 | 这是最初的核心需求之一。上游**也没有**这个功能（`api_rule_create` 强制要求 `source_rule_id`，只能复制母版，且其 AI 生成 prompt 明确禁止"复刻官方规则书 RAW"）。目前只能手写 `custom_mechanics` JSON |
| 前端 host 组件 | ❌ 未做 | Stage B 的 `available_intents` 暂无 UI 消费；当前 Stage A 不需要 |
| `character_lifecycle="rules_aware"` | ❌ 未启用 | 因此存档尚未写入 `rule_binding`（`bind_ruleset_runtime` 未被调用）。启用需实现 `ruleset_character` + `rule_binding`，范本见 `src/rulesets/dnd2024/character/builder.py` |
| 资源下限 | ⚠️ 固定为 0 | `delta` 的负向下限目前写死 0，未做成声明式 |
| 内置规则的枚举天花板 | ⚠️ 仍在 | `check_mechanic` 那 6 个枚举对**非** `custom:declarative` 的规则依然生效。若想让叙事检定路径也开放，需做"枚举注册表化"（改 `src/engine/checks.py` 的 dispatch） |
| 内容包禁止代码执行 | ✅ 有意保留 | `src/rulesets/bundle.py` 的 `FORBIDDEN_EXECUTION_KEYS` 拒绝 `python`/`javascript`/`eval` 等键。**不要为了让规则更灵活而拆这道墙**，正路是"加声明类型 + 加代码分支" |
| 单进程限制 | ⚠️ 继承自上游 | 三层锁是进程内 asyncio 锁，存档是 JSON + SQLite 单写者 → **不能多 worker 横向扩展**。朋友团足够，多租户 SaaS 会撞墙 |

---

## 4. 下一步计划（按优先级）

1. **规则书导入管线**（用户核心需求，两个上游都没有）
   - 需要先确定规则书格式：PDF 文本层 / 扫描件（需 OCR）/ Word / 网页 / 手打 txt
   - 设计取向：抽取结果直接产出 `custom_mechanics` + 属性表，复用 `manifest.py` 的校验与 `data/templates/rules/` 落盘
   - 必须保留出处（`{file, page}`）以便人工校对
   - 注意版权：只做本地个人使用，**不要提交进开源仓库**
2. 用真实规则替换 `custom_freeform.json`，在 Stage A 下调数值手感
3. 需要"必须有确定结果"的机制时再上 Stage B + 前端 host 组件
4. 启用 `character_lifecycle="rules_aware"`，让存档写入 `rule_binding`
5. 可选：把叙事检定路径的 6 个枚举改成注册表（一次性打开所有未来机制）

---

## 5. 二开约束速查（改代码前先看）

来自上游 `docs/ENGINEERING_RULES.md` 与 `tests/architecture/`：

1. **`rulesets` 不得依赖 `src.webui` / `src.compat` / `src.web_transport`** —— AST 守卫会失败
2. **不要 `from src.engine.modules import ...`** —— 该包 `__init__` 拉入 lorebook 存储并形成导入环；
   直接读写 `instance.ruleset_state` / `instance.event_ledger` 属性（D&D 2024 也这么做）
3. **`memory_deltas_from_event_batch` 必须存在** —— 权威路径无条件调用，删了会 AttributeError
4. **改 `STATE_SCHEMA_VERSION` 必须同步加 `migrate_state` 分支** —— 这是持久化契约
5. **前端规则扩展只能注册在 `frontend-v2/src/features/rulesets/registry.ts`** —— 它是唯一的
   concrete-ruleset 豁免点（见 `scripts/architecture_fitness.py`）
6. **改架构边界属于 Level 1–2 稳定面** —— 按仓库规矩需要写 Design Note 说明契约与风险
7. **LLM 不是 authority** —— 叙事可以描述结果，数值只能经 `apply_event_batch` 变化

---

## 6. 关键环境事实（换机器时容易踩）

| 事实 | 说明 |
|---|---|
| Python 实际最低 3.11 | 上游 README 写 3.10 不准（用了 `typing.NotRequired`）。实测 3.10 直接 ImportError |
| 导入顺序脆弱 | 进程若最先导入 `src.rulesets` 或 `src.engine.modules` 会撞既有导入环；先 `import web_server` 即正常。单独跑 pytest 文件需 `-p web_server` |
| npm 必须用 `.cmd` | Windows PowerShell 执行策略拒绝 `npm.ps1` |
| npm 官方源很慢 | 实测单包 158 秒；换 `registry.npmmirror.com` 后整轮 56 秒 |
| `static-v2/` 克隆后为空 | 只含 favicon / sponsor 图 / `_redirects`，**前端必须构建**，否则 `/` 返回 404 |
| Dockerfile 默认 target 是 `runtime` | 本地 `docker compose build` 用默认（自包含）；`managed-artifact` 是 CI 专用 |
| 模型配置必须走 WebUI | 旧 `TRPG_LLM_*` 等环境变量已废弃并返回 400 |
| Base URL 要写到 `/v1` | 代码会补 `/chat/completions`；Anthropic 格式填 `https://api.anthropic.com`（拼 `/v1/messages`） |
