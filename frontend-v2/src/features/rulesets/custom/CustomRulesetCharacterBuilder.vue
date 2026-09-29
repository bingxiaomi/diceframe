<script setup lang="ts">
/**
 * 声明式规则（profile="custom"）的建卡器。
 *
 * 与 D&D 那套的区别：**没有职业表、没有法术、没有装备包**。声明式规则的建卡
 * 素材全部来自 `custom_mechanics` 声明（属性范围、点购预算、资源、检定），
 * 所以这个组件只做四件事：填身份、分配属性、挑技能、提交。
 *
 * 提交前一定走服务端的 `validate` + `finalize`：派生值（hp/armor_class）由服务端
 * 算，客户端算出来的一律会被丢弃 —— 这一点在 `normalize_character_submission`
 * 里有测试钉着。
 */
import { computed, onMounted, ref, watch } from 'vue'
import type { CharacterSheet, JsonObject } from '@/api/types'
import {
  deriveRulesetBuilderCharacter,
  fetchRulesetBuilderChoices,
  finalizeRulesetBuilderCharacter,
  validateRulesetBuilderDraft,
} from '@/api/rulesets'
import type { CustomBuilderChoices, CustomQuickPreset } from './types'

const props = withDefaults(defineProps<{
  ruleId: string
  language?: string
  initial?: CharacterSheet
  embedded?: boolean
}>(), { language: 'zh-CN', embedded: false })
const emit = defineEmits<{ submit: [character: CharacterSheet]; cancel: [] }>()

const emptyChoices = (): CustomBuilderChoices => ({
  profile: 'custom', attributes: [], attribute_points: 0, attribute_default: 10,
  attr_hint: '', races: [], classes: [], skills: [], max_skills: 0, skill_hint: '',
  resources: [], checks: [], quick_presets: [],
})

const zh = computed(() => String(props.language || '').toLowerCase().startsWith('zh'))
const text = (cn: string, en: string) => zh.value ? cn : en

const choices = ref<CustomBuilderChoices>(emptyChoices())
const step = ref(1)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const errors = ref<string[]>([])

const name = ref('')
const race = ref('')
const klass = ref('')
const background = ref('')
const attributes = ref<Record<string, number>>({})
const skills = ref<string[]>([])

const steps = computed(() => [
  { id: 1, label: text('身份', 'Identity') },
  { id: 2, label: text('属性', 'Attributes') },
  { id: 3, label: text('技能与审核', 'Skills & review') },
])

const spent = computed(() => choices.value.attributes.reduce(
  (total, spec) => total + (attributes.value[spec.key] ?? spec.min) - spec.min, 0,
))
const budget = computed(() => choices.value.attribute_points)
const budgetLeft = computed(() => budget.value - spent.value)
const overBudget = computed(() => budget.value > 0 && budgetLeft.value < 0)

function draft(): JsonObject {
  return {
    name: name.value.trim(),
    race: race.value,
    class: klass.value,
    background: background.value.trim(),
    attributes: { ...attributes.value },
    skills: [...skills.value],
    locale: props.language || 'zh-CN',
  }
}

function seedAttributes(): void {
  const next: Record<string, number> = {}
  for (const spec of choices.value.attributes) {
    next[spec.key] = spec.min
  }
  attributes.value = next
}

function applyPreset(preset: CustomQuickPreset): void {
  const incoming = preset.draft || {}
  const incomingAttributes = incoming.attributes as Record<string, number> | undefined
  name.value = String(incoming.name || preset.name || '')
  race.value = String(incoming.race || '')
  klass.value = String(incoming.class || '')
  background.value = String(incoming.background || '')
  if (incomingAttributes) attributes.value = { ...incomingAttributes }
  const incomingSkills = incoming.skills
  skills.value = Array.isArray(incomingSkills) ? incomingSkills.map(String) : []
  step.value = 3
}

function shift(spec: CustomBuilderChoices['attributes'][number], delta: number): void {
  const current = attributes.value[spec.key] ?? spec.min
  const next = Math.max(spec.min, Math.min(spec.max, current + delta))
  if (next === current) return
  if (delta > 0 && budget.value > 0 && budgetLeft.value <= 0) return
  attributes.value = { ...attributes.value, [spec.key]: next }
}

function toggleSkill(id: string): void {
  if (skills.value.includes(id)) {
    skills.value = skills.value.filter(item => item !== id)
    return
  }
  if (choices.value.max_skills > 0 && skills.value.length >= choices.value.max_skills) return
  skills.value = [...skills.value, id]
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const response = await fetchRulesetBuilderChoices(props.ruleId, draft(), props.language)
    choices.value = { ...emptyChoices(), ...(response.choices as unknown as CustomBuilderChoices) }
    seedAttributes()
    const initial = props.initial as JsonObject | undefined
    if (initial) {
      name.value = String(initial.character_name || initial.name || '')
      const stored = initial.attributes as Record<string, number> | undefined
      if (stored) attributes.value = { ...attributes.value, ...stored }
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

async function review(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const response = await validateRulesetBuilderDraft(props.ruleId, draft(), props.language)
    errors.value = response.errors || []
    if (!errors.value.length) step.value = 3
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busy.value = false
  }
}

async function preview(): Promise<string> {
  const response = await deriveRulesetBuilderCharacter(props.ruleId, draft(), props.language)
  const derived = (response.character.derived || {}) as Record<string, number>
  return text(
    `生命 ${derived.hp ?? '?'} / 上限 ${derived.max_hp ?? '?'}，护甲 ${derived.armor_class ?? '?'}`,
    `HP ${derived.hp ?? '?'} / max ${derived.max_hp ?? '?'}, AC ${derived.armor_class ?? '?'}`,
  )
}

const summary = ref('')

async function submit(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    // 派生值由服务端算：客户端这里只送玩家做的选择。
    summary.value = await preview()
    const response = await finalizeRulesetBuilderCharacter(props.ruleId, draft(), props.language)
    emit('submit', response.character)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    busy.value = false
  }
}

onMounted(load)
watch(() => props.ruleId, load)
</script>

<template>
  <section class="custom-builder" :class="{ embedded }">
    <p v-if="loading" class="hint">{{ text('正在读取规则声明…', 'Loading rules declaration…') }}</p>
    <p v-else-if="error" class="error-banner" role="alert">{{ error }}</p>

    <template v-else>
      <ol class="rail" :aria-label="text('建卡步骤', 'Builder steps')">
        <li v-for="item in steps" :key="item.id" :class="{ active: step === item.id }">
          <button type="button" @click="step = item.id">{{ item.label }}</button>
        </li>
      </ol>

      <div v-if="step === 1" class="pane">
        <label>
          <span>{{ text('角色名', 'Name') }}</span>
          <input v-model="name" type="text" :placeholder="text('冒险者', 'Adventurer')">
        </label>
        <label v-if="choices.races.length">
          <span>{{ text('种族', 'Race') }}</span>
          <select v-model="race">
            <option value="">—</option>
            <option v-for="row in choices.races" :key="row.id" :value="row.id">{{ row.name }}</option>
          </select>
        </label>
        <label v-if="choices.classes.length">
          <span>{{ text('职业', 'Class') }}</span>
          <select v-model="klass">
            <option value="">—</option>
            <option v-for="row in choices.classes" :key="row.id" :value="row.id">{{ row.name }}</option>
          </select>
        </label>
        <label>
          <span>{{ text('背景', 'Background') }}</span>
          <input v-model="background" type="text">
        </label>

        <div v-if="choices.quick_presets.length" class="presets">
          <p class="hint">{{ text('或者从预设开始', 'Or start from a preset') }}</p>
          <button
            v-for="preset in choices.quick_presets"
            :key="preset.id"
            type="button"
            @click="applyPreset(preset)"
          >{{ preset.name }}</button>
        </div>
      </div>

      <div v-else-if="step === 2" class="pane">
        <p class="hint">{{ choices.attr_hint || text('按规则声明的范围与预算分配。', 'Allocate within the declared ranges and budget.') }}</p>
        <p class="budget" :class="{ over: overBudget }">
          {{ text('剩余点数', 'Points left') }}: {{ budget > 0 ? budgetLeft : '—' }}
        </p>
        <div class="attr-grid">
          <div v-for="spec in choices.attributes" :key="spec.key" class="attr-row">
            <span class="attr-name">{{ spec.name }}</span>
            <button type="button" :disabled="(attributes[spec.key] ?? spec.min) <= spec.min"
              @click="shift(spec, -1)">−</button>
            <output>{{ attributes[spec.key] ?? spec.min }}</output>
            <button type="button" :disabled="(attributes[spec.key] ?? spec.min) >= spec.max || overBudget"
              @click="shift(spec, 1)">+</button>
            <span class="range">{{ spec.min }}–{{ spec.max }}</span>
          </div>
        </div>
      </div>

      <div v-else class="pane">
        <p v-if="choices.skill_hint" class="hint">{{ choices.skill_hint }}</p>
        <div v-if="choices.skills.length" class="skills">
          <button
            v-for="row in choices.skills"
            :key="row.id"
            type="button"
            :class="{ chosen: skills.includes(row.id) }"
            @click="toggleSkill(row.id)"
          >{{ row.name }}</button>
        </div>
        <p class="hint">
          {{ text('已选', 'Chosen') }}: {{ skills.length }}
          <template v-if="choices.max_skills">/ {{ choices.max_skills }}</template>
        </p>
        <p v-if="summary" class="summary">{{ summary }}</p>
        <ul v-if="errors.length" class="errors">
          <li v-for="(item, index) in errors" :key="index">{{ item }}</li>
        </ul>
      </div>

      <footer class="actions">
        <button v-if="step > 1" type="button" @click="step -= 1">{{ text('上一步', 'Back') }}</button>
        <button v-if="step < 3" type="button" :disabled="busy" @click="step === 2 ? review() : step = 2">
          {{ text('下一步', 'Next') }}
        </button>
        <button v-else type="button" class="primary" :disabled="busy" @click="submit">
          {{ text('完成建卡', 'Finish') }}
        </button>
        <button type="button" class="ghost" @click="emit('cancel')">{{ text('取消', 'Cancel') }}</button>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.custom-builder { display: grid; gap: 12px; }
.rail { display: flex; gap: 8px; list-style: none; margin: 0; padding: 0; }
.rail button { border: 1px solid var(--border, #444); background: transparent; padding: 4px 10px; border-radius: 6px; cursor: pointer; }
.rail .active button { border-color: var(--accent, #7aa2f7); }
.pane { display: grid; gap: 10px; }
.pane label { display: grid; gap: 4px; }
.attr-grid { display: grid; gap: 6px; }
.attr-row { display: flex; align-items: center; gap: 8px; }
.attr-name { min-width: 4rem; }
.attr-row output { min-width: 2rem; text-align: center; font-variant-numeric: tabular-nums; }
.range { opacity: 0.6; font-size: 0.85em; }
.skills, .presets { display: flex; flex-wrap: wrap; gap: 6px; }
.skills button.chosen { outline: 2px solid var(--accent, #7aa2f7); }
.budget.over { color: #f7768e; }
.hint { opacity: 0.75; font-size: 0.9em; margin: 0; }
.summary { font-variant-numeric: tabular-nums; }
.errors { color: #f7768e; margin: 0; padding-left: 1.2em; }
.actions { display: flex; gap: 8px; justify-content: flex-end; }
</style>
