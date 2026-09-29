/**
 * 声明式规则（profile="custom" / runtime="custom:declarative"）的共享类型。
 *
 * **为什么不复用 `@/api/types` 里的 `RulesetGameplayView`**：那个类型描述的是
 * D&D 的形状（`combat` / `encounter_presets` 是必填），而声明式运行时的投影
 * 是 `seats` / `declared_checks` / `counters`。硬把两者塞进一个接口会让每个字段
 * 都变成可选，那样两边都失去类型保护。`runtime_id` 是区分它们的地方，所以在调用
 * 边界做一次显式转换（见 `CustomRulesetPanel.vue` 的 `load`）。
 */

export interface CustomNameRow {
  id: string
  name: string
}

export interface CustomAttributeSpec {
  key: string
  name: string
  min: number
  max: number
}

export interface CustomQuickPreset extends CustomNameRow {
  summary?: string
  draft: Record<string, unknown>
}

export interface CustomBuilderChoices {
  profile: string
  attributes: CustomAttributeSpec[]
  attribute_points: number
  attribute_default: number
  attr_hint: string
  races: CustomNameRow[]
  classes: CustomNameRow[]
  skills: CustomNameRow[]
  max_skills: number
  skill_hint: string
  resources: Array<{ id: string; name: string; default: number }>
  checks: Array<{ id: string; name: string; dice: string }>
  quick_presets: CustomQuickPreset[]
}

/** 目标达成情况。`partial` 由服务端派生（达成 + 程度在开区间内）。 */
export interface CustomGoalOutcome {
  achieved: boolean
  extent: number
  partial: boolean
}

export interface CustomCostLine {
  resource: string
  amount: number
  timing: string
  reason: string
}

/** 一次裁定的完整结果。判断该读 `goal` / `severity`，`degree` 只是规则书的名字。 */
export interface CustomOutcomeBreakdown {
  degree: string
  degree_label: string
  resolution: string
  goal: CustomGoalOutcome
  severity: string
  costs: CustomCostLine[]
}

export interface CustomTraceEntry {
  rule: string
  kind: string
  reason: string
  detail: Record<string, unknown>
}

/** `custom.check.resolved` 记录。没掷骰时没有 `outcome`（不编一个）。 */
export interface CustomCheckRecord {
  type: string
  check_id?: string
  check_name?: string
  total?: number
  target?: number
  degree?: string
  degree_label?: string
  is_success?: boolean
  resolution?: string
  reason?: string[]
  outcome?: CustomOutcomeBreakdown
  trace?: CustomTraceEntry[]
}

export interface CustomResourceView {
  id: string
  name: string
  /** **有效值**（基础值 + 活跃效果）。 */
  value: number
  /** 只在有效值与基础值不同时出现 —— 界面靠它显示「44 → 39」。 */
  base?: number
  max?: number
}

export interface CustomSeatEffect {
  id: string
  source: string
  note: string
  duration: { type: string; remaining: number } | null
  changes: Array<{ key: string; op: string; value: unknown }>
}

export interface CustomSeat {
  player_id: string
  name: string
  is_self: boolean
  resources?: Record<string, CustomResourceView>
  effects?: CustomSeatEffect[]
  /** 别人的席位只给摘要（隐私边界）。 */
  resource_count?: number
}

export interface CustomGameplayView {
  runtime_id: string
  state_version: number
  seats: CustomSeat[]
  counters?: Record<string, unknown>
  flags?: Record<string, unknown>
  declared_checks?: Array<{ id: string; name: string; dice: string }>
}

export interface CustomAvailableAction {
  type: string
  check_id?: string
  resource_id?: string
  label: string
  dice?: string
}

export interface CustomGameplayResponse {
  ok: boolean
  game_key?: string
  rule_id?: string
  gameplay: CustomGameplayView
  available_actions?: CustomAvailableAction[]
  result?: {
    recorded_events?: CustomCheckRecord[]
    replayed?: boolean
    [key: string]: unknown
  }
}
