# 裁定契约（Adjudication Contract）

> 代码：`src/rulesets/adjudication.py` · 测试：`tests/rulesets/test_adjudication.py`（47 项）
>
> 这一层解决的是"**谁赢了由谁说了算，以及怎么说给别人听**"。

## 1. 四份借鉴，各管一段

| 来源 | 借什么 | 落到本模块 |
| --- | --- | --- |
| **D&D 2024 基础规则** | 判断程序：可能成功 + 可能失败 + 失败有代价 → 才掷骰 | `three_question_resolution()` |
| **Foundry VTT dnd5e** | 程序化表示：`Activity`（可执行单元）、`ActiveEffect`（基础值 + 修正量 = 有效值） | `Activity`、`EffectDescriptor`、`EFFECT_CHANGE_OPS` |
| **Pathfinder 2e** | 结果分级：critical success / success / failure / critical failure | `OutcomeDegree`（+ 一档 `PARTIAL_SUCCESS`） |
| **Blades in the Dark** | 自由意图的裁定：先定 Position（风险）与 Effect（效果），再决定怎么掷 | `Risk`、`Effect` |

**借结构，不抄代码。** 机制与判断程序不受版权保护，规则书**原文文本**受保护 ——
所以本仓库只搬运结构与词汇，商业规则书正文放到 `data/`（已被 `.gitignore` 覆盖，不入库）。

## 2. 控制流词汇必须保持小

`Resolution` **只有 5 个值**，因为宿主只靠它决定控制流：

| 值 | 宿主做什么 | 例子 |
| --- | --- | --- |
| `AUTO_SUCCESS` | 不掷骰，叙述成功 | 打开一扇没锁的门 |
| `AUTO_FAILURE` | 不掷骰，叙述失败 | 说服国王送出国土 |
| `IMPOSSIBLE` | 不掷骰，叙述"做不到" | 徒手推倒城墙 |
| `CHECK_REQUIRED` | 走掷骰路径 | 火灾里 20 秒撬锁 |
| `NEEDS_CLARIFICATION` | 回问玩家（**少用**） | 目标或手段缺失 |

派生信号（不用再加枚举）：

```python
adjudication.requires_check        # 要不要掷骰
adjudication.needs_clarification   # 要不要回问
adjudication.outcome_sign          # +1 / 0 / -1，叙述基调
```

规则集那些细腻档位（极难成功 / 大成功 / fumble / raise / complication）
统统属于 `OutcomeDegree` 和事件 payload，**不属于控制流**。

> 测试 `test_resolution_vocabulary_stays_small` 会守住这条。加档位前先问：
> 宿主会因此走不同分支吗？不会就别加。

## 3. 三问法

```
can_succeed        can_fail    failure_matters   结果
True               False       —                 AUTO_SUCCESS    打开没锁的门
False              —           —                 IMPOSSIBLE      推倒城墙
True               True        False             AUTO_SUCCESS    无限时间反复开锁
True               True        True              CHECK_REQUIRED  火灾里 20 秒撬锁
```

**"失败有意义"这一条堵掉了刷骰**：如果玩家能无限重试，总有一次成功，
那这个骰子实际上没有意义 —— 应该给 `AUTO_SUCCESS`，而不是允许"我再检查一次"。

副作用要注意：一旦引入"已尝试过 → 自动成功"，GM 手动改世界状态时必须能
**清空尝试记录**（把门重新锁上）。尝试计数放 `ruleset_state`，不要放 `event_ledger`
（D&D 路径不裁剪账本）。

## 4. 风险 / 效果：环境决定风险，不是角色数值

Blades 的洞见是 **Position 由环境决定**：

```
"我冲过去，一个人正面攻击五个守卫"   → Risk: EXTREME, Effect: LIMITED
"我退进只能同时通过一人的窄巷"       → Risk: HIGH,    Effect: LIMITED
```

同样的角色，同样的数值 —— 是**做法**改变了风险。这对 AI GM 特别重要，
因为它让系统能理解"为什么这个做法更好"。

**这两项是运行时内部语义层，不是要规则集放弃自己的 DC 体系。** 它们最终映射：

```python
Risk.STANDARD.dc_modifier            # -4 / 0 / +4 / +8
Risk.HIGH.grants_disadvantage        # 高风险默认给劣势
Effect.LIMITED.degree_shift          # 把 CRITICAL_SUCCESS 压成 SUCCESS
Effect.GREAT.degree_shift            # 把 SUCCESS 抬成 CRITICAL_SUCCESS
```

## 5. 效果描述符：不要直接改基础数值

`EffectDescriptor` 的形状对齐 Foundry 的 ActiveEffect change：

```python
EffectDescriptor(
    kind="resource",        # resource | attribute | condition | item | world | score
    target="pc_01",
    field="resolve",
    op="subtract",          # add|subtract|multiply|override|upgrade|downgrade
    value="1d6",
    duration={"type": "round", "remaining": 8},
    source="intent:abc",    # 溯源：对应 Foundry 的 origin
)
```

**为什么不能直接改 `attributes`**：一旦把 `bless` 写进 `dex`，以后就再也分不清
哪部分是角色底子、哪部分是一次性加值。正确的形状是

```
Base Character  +  Active Effects  =  Effective Character
```

`source` 是溯源。没有它就无法回答"这个 +1d4 是哪来的、什么时候到期"。

## 6. Stakes：约束事实，不约束文笔

```python
Stakes(
    success=(EffectDescriptor(op="override", field="door_open", value=True),),
    partial=(EffectDescriptor(kind="world", field="noise", op="override", value=True),),
    failure=(EffectDescriptor(kind="resource", field="resolve", op="subtract", value="1d6"),),
)
```

三层职责：

- 权威层决定 **发生了什么**（事件与效果）
- `stakes` 告诉叙事层 **必须提到什么**（失败的代价不能漏写）
- 叙事层仍然**自由**决定 **怎么讲**（文风、细节、节奏）

**约束了文笔就变模板文了，体验反而更差。** 所以 `stakes` 是事实清单，不是文案模板。

`PARTIAL_SUCCESS` 就是为这一层准备的：

> 你成功撬开门，但响声惊动了里面的人。

另一条反面做法要明确排除：**别用 prompt 做这件事**（"你是严格 DM，不要被大成功
冲昏头脑"）。那是软约束，模型会漂移，而且那正是要修的病。约束必须在第一层。

## 7. 两条硬约束

### 7.1 客户端不能伪造骰子

`Activity` 里**没有任何"掷出多少"的字段**；`intent_field_violations()` 递归检查
意图 payload 是否含结果字段：

```python
intent_field_violations({"type": "custom.check.roll", "d20": 20, "total": 25})
# → ["d20", "total"]

intent_field_violations({"payload": {"damage": {"roll": "2d6"}}})
# → ["payload.damage.roll"]     藏在嵌套里也会被抓到
```

`custom:declarative` 的 `validate_intent` 已接上它，返回 `CLIENT_ROLL_FORBIDDEN`。

**刻意不含 `dice` / `modifier` / `advantage` / `disadvantage`**：那些是描述符
（用什么骰子、有没有加值），不是结果，而且可能出现在 `available_intents` 的
返回值里被前端回传。一并禁掉只会误伤合法流程。

### 7.2 非法裁定构造不出来

`Adjudication.validate()` 会拒绝：

| 情况 | 为什么是错的 |
| --- | --- |
| `CHECK_REQUIRED` 但没给 `activity` | 宿主不知道掷什么 |
| `CHECK_REQUIRED` 的 activity 没有 `dc`/`skill`/`ability` | 没有目标数 |
| `AUTO_SUCCESS` 配 `attack` | 自相矛盾：那还需要掷骰 |
| 非 `NEEDS_CLARIFICATION` 却没有 `reason` | 叙事层无从解释，玩家只看到"系统说不行" |

## 8. 承载方式：复用既有事件类型

裁定结果就是 **`check.resolved`** 事件的 payload —— 不新造外壳。
这个类型在 D&D 的 reducer 里本来就是 no-op，所以加它不会打破任何现有路径。

```python
payload = adjudication.to_event(source="intent:abc")
restored = Adjudication.from_event(payload)
assert restored == adjudication          # 往返不掉信息
```

## 9. 这一层不做策略

`adjudication.py` **只做词汇、校验与转换**。真正的"算"在 runtime 的
`resolve_intent` 里（阶段 2）：

```
Intent（Goal + Approach）        ← 阶段 3：LLM 解释层（必须是独立 async 协议）
        ↓
Authority                        ← 阶段 2：三问法 + 胜任度 + 风险/效果
        ↓
Activity（check / attack / save / use …）
        ↓
Roll（服务端 RNG）
        ↓
OutcomeDegree
        ↓
Event（check.resolved + 世界变化）
        ↓
Narrator                          ← 宿主侧，只负责讲
```

## 10. 尚未处理

- **尝试记录 / 防刷骰的落盘结构**（`ruleset_state["attempts"]`，含 `reset` 语义）
- **世界状态对象**：现在 `ruleset_state` 只有 `resources`，没有场景对象（门 / 锁 / 守卫），
  所以 `EffectDescriptor(kind="world")` 暂时落不了地
- **ActiveEffect 的求值**：`EffectDescriptor` 只是声明，还没有"基础值 + 效果 = 有效值"
  的投影实现
- **`ConsumptionError` 对应物**：资源不足应让整次裁定失败，目前只是约定常量
  `CONSUMPTION_FAILED`
