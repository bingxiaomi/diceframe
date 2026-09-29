<script setup lang="ts">
/**
 * 声明式规则（runtime="custom:declarative"）的「规则与检定」面板。
 *
 * 这个面板是本项目里**唯一**能让玩家真正触发权威裁定的界面：提交
 * `custom.check.roll`，由服务端掷骰、落账、回传裁定结果。
 *
 * 它显示三件别处看不到的东西：
 *
 * 1. **有效值**（基础值 + 活跃效果）—— 以及它的来路与剩余时长；
 * 2. **结果分解** —— 成没成 / 成了多少 / 有多疼 / 付了什么；
 * 3. **规则追踪** —— 这一次裁定里哪条规则为什么触发。
 *
 * 三者都是服务端算好的事实，界面只负责展示，不重新解释规则。
 */
import { computed, onMounted, ref, watch } from 'vue'
import type { JsonObject } from '@/api/types'
import { fetchRulesetAvailableActions, submitRulesetIntent } from '@/api/rulesets'
import { useLocale } from '@/composables/useLocale'
import type {
  CustomAvailableAction,
  CustomCheckRecord,
  CustomGameplayResponse,
  CustomSeat,
} from './types'

const props = withDefaults(defineProps<{
  gameKey: string
  actorId: string
  characterName?: string
  isGm?: boolean
  refreshKey?: number
}>(), { characterName: '', isGm: false, refreshKey: 0 })
const emit = defineEmits<{ refresh: [] }>()

const { locale } = useLocale()
const zh = computed(() => String(locale.value || '').toLowerCase().startsWith('zh'))
const text = (cn: string, en: string) => zh.value ? cn : en

const CHECKS = 'custom.check.roll'
const ADJUST = 'custom.resource.adjust'

const data = ref<CustomGameplayResponse | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const record = ref<CustomCheckRecord | null>(null)
const showTrace = ref(false)
const adjustResource = ref('')
const adjustDelta = ref(-1)

const seats = computed<CustomSeat[]>(() => data.value?.gameplay?.seats || [])
const selfSeat = computed(() => seats.value.find(seat => seat.is_self) || null)
const otherSeats = computed(() => seats.value.filter(seat => !seat.is_self))
const actions = computed<CustomAvailableAction[]>(() => data.value?.available_actions || [])
const checks = computed(() => actions.value.filter(action => action.type === CHECKS))
const resources = computed(() => actions.value.filter(action => action.type === ADJUST))
const stateVersion = computed(() => Number(data.value?.gameplay?.state_version || 0))

function newIntentId(): string {
  const cryptoApi = globalThis.crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  return `intent-${Date.now()}-${Math.floor(Math.random() * 1e6)}`
}

function describeDuration(effect: { duration: { type: string; remaining: number } | null }): string {
  if (!effect.duration) return text('永久', 'permanent')
  return `${effect.duration.type} × ${effect.duration.remaining}`
}

function amountText(cost: { resource: string; amount: number }): string {
  return `−${cost.amount} ${cost.resource}`
}

async function load(silent = false): Promise<void> {
  const gameKey = props.gameKey
  if (!gameKey) return
  if (!silent) loading.value = true
  try {
    // 共享的 `RulesetGameplayResponse` 描述的是 D&D 的形状（combat 等为必填）；
    // 声明式运行时的投影是 seats / declared_checks。两者靠 runtime_id 区分，
    // 所以这里做一次显式转换，而不是把两边都改成"全是可选字段"。
    const response = await fetchRulesetAvailableActions(gameKey) as unknown as CustomGameplayResponse
    if (props.gameKey !== gameKey) return
    data.value = response
    // 静默纠偏**不清错误**：那条消息就是用户刚触发失败的原因，
    // 擦掉它会让"点了没反应"变成最难查的那种反馈。
    if (!silent) error.value = ''
    if (!adjustResource.value && resources.value.length) {
      adjustResource.value = String(resources.value[0].resource_id || '')
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  } finally {
    loading.value = false
  }
}

async function submit(intent: JsonObject): Promise<void> {
  const gameKey = props.gameKey
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const response = await submitRulesetIntent(gameKey, {
      ...intent,
      // 意图标识。**注意：声明式运行时目前还没有按它去重** —— 重放同一个
      // intent_id 会再掷一次骰、再扣一次资源（见
      // tests/rulesets/test_custom_gameplay_http.py 里那条 xfail）。
      // 这里先按最终形状发出去，等服务端补上就不必再改客户端。
      intent_id: newIntentId(),
    }) as unknown as CustomGameplayResponse
    if (props.gameKey !== gameKey) return
    data.value = response
    const recorded = response.result?.recorded_events || []
    record.value = recorded.find(item => item.type === 'custom.check.resolved') || null
    if (response.result?.replayed) notice.value = text('重放：没有掷新的骰子。', 'Replayed: no new roll.')
    emit('refresh')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    // 失败后用服务端状态纠偏：本地很可能已经不是权威视图了。
    // （load(true) 刻意不清 error，见那里的注释。）
    await load(true)
  } finally {
    busy.value = false
  }
}

function roll(action: CustomAvailableAction): void {
  void submit({ type: CHECKS, check_id: action.check_id })
}

function adjust(): void {
  const delta = Number(adjustDelta.value)
  if (!adjustResource.value || !Number.isFinite(delta) || delta === 0) return
  void submit({ type: ADJUST, resource_id: adjustResource.value, delta })
}

onMounted(() => load())
watch(() => props.refreshKey, () => load(true))
watch(() => props.gameKey, () => load())
</script>

<template>
  <section class="custom-rules-panel">
    <header class="panel-head">
      <span class="version">{{ text('状态版本', 'State version') }} {{ stateVersion }}</span>
      <button type="button" :disabled="loading || busy" @click="load()">
        {{ text('刷新', 'Refresh') }}
      </button>
    </header>

    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice">{{ notice }}</p>
    <p v-if="loading && !data" class="hint">{{ text('正在读取规则状态…', 'Loading rules state…') }}</p>

    <template v-if="selfSeat">
      <section class="block">
        <h3>{{ text('我的资源', 'My resources') }}</h3>
        <ul class="resources">
          <li v-for="(resource, key) in selfSeat.resources || {}" :key="key">
            <span class="name">{{ resource.name }}</span>
            <span class="value">
              <template v-if="resource.base !== undefined">
                <!-- 有效值 ≠ 基础值：把来路显出来，否则玩家只会看到数字自己变了 -->
                <span class="base">{{ resource.base }}</span> →
              </template>
              {{ resource.value }}
              <template v-if="resource.max !== undefined"><span class="max">/ {{ resource.max }}</span></template>
            </span>
          </li>
        </ul>

        <ul v-if="selfSeat.effects?.length" class="effects">
          <li v-for="effect in selfSeat.effects" :key="effect.id">
            <span class="name">{{ effect.note || effect.id }}</span>
            <span class="source">{{ effect.source }}</span>
            <span class="duration">{{ describeDuration(effect) }}</span>
            <span class="changes">
              <template v-for="(change, index) in effect.changes" :key="index">
                {{ change.op }} {{ change.value }} → {{ change.key }}
              </template>
            </span>
          </li>
        </ul>
      </section>

      <section class="block">
        <h3>{{ text('检定', 'Checks') }}</h3>
        <p v-if="!checks.length" class="hint">{{ text('本规则没有声明检定。', 'No checks declared.') }}</p>
        <div class="checks">
          <button
            v-for="action in checks"
            :key="action.check_id"
            type="button"
            :disabled="busy"
            :data-testid="`custom-check-${action.check_id}`"
            @click="roll(action)"
          >
            <span>{{ action.label }}</span>
            <small v-if="action.dice">{{ action.dice }}</small>
          </button>
        </div>
      </section>

      <section v-if="isGm && resources.length" class="block">
        <h3>{{ text('GM 调整资源', 'GM resource adjust') }}</h3>
        <div class="adjust">
          <select v-model="adjustResource">
            <option v-for="resource in resources" :key="resource.resource_id" :value="resource.resource_id">
              {{ resource.label }}
            </option>
          </select>
          <input v-model.number="adjustDelta" type="number" step="1">
          <button type="button" :disabled="busy || !adjustResource" @click="adjust">
            {{ text('应用', 'Apply') }}
          </button>
        </div>
      </section>
    </template>

    <section v-if="record" class="block result" data-testid="custom-last-outcome">
      <h3>{{ text('最近一次裁定', 'Last adjudication') }}</h3>
      <p class="headline">
        <span class="degree">{{ record.degree_label || record.resolution }}</span>
        <span v-if="record.total !== undefined" class="dice">
          {{ record.total }} / {{ record.target }}
        </span>
      </p>

      <ul v-if="record.reason?.length" class="reasons">
        <li v-for="(line, index) in record.reason" :key="index">{{ line }}</li>
      </ul>

      <template v-if="record.outcome">
        <p class="goal">
          <span :class="record.outcome.goal.achieved ? 'ok' : 'bad'">
            {{ record.outcome.goal.achieved
              ? (record.outcome.goal.partial ? text('部分达成', 'Partially achieved') : text('达成', 'Achieved'))
              : text('未达成', 'Not achieved') }}
          </span>
          <span v-if="record.outcome.goal.achieved" class="extent">
            {{ Math.round(record.outcome.goal.extent * 100) }}%
          </span>
          <span v-if="record.outcome.severity !== 'NONE'" class="severity">
            {{ text('后果', 'Severity') }}: {{ record.outcome.severity }}
          </span>
        </p>
        <ul v-if="record.outcome.costs.length" class="costs">
          <li v-for="(cost, index) in record.outcome.costs" :key="index">
            {{ amountText(cost) }} · {{ cost.timing }}
          </li>
        </ul>
      </template>

      <template v-if="record.trace?.length">
        <button type="button" class="trace-toggle" @click="showTrace = !showTrace">
          {{ showTrace ? text('收起推理链', 'Hide trace') : text('为什么？', 'Why?') }}
        </button>
        <ol v-if="showTrace" class="trace">
          <li v-for="(entry, index) in record.trace" :key="index">
            <code>{{ entry.rule }}</code>
            <span class="kind">{{ entry.kind }}</span>
            <span class="reason">{{ entry.reason }}</span>
          </li>
        </ol>
      </template>
    </section>

    <section v-if="otherSeats.length" class="block">
      <h3>{{ text('其他席位', 'Other seats') }}</h3>
      <ul class="others">
        <li v-for="seat in otherSeats" :key="seat.player_id">
          <span class="name">{{ seat.name }}</span>
          <!-- 别人的数值是隐私边界：只报数量，不给具体值 -->
          <span class="count">{{ seat.resource_count ?? 0 }} {{ text('项资源', 'resources') }}</span>
        </li>
      </ul>
    </section>
  </section>
</template>

<style scoped>
.custom-rules-panel { display: grid; gap: 14px; overflow: auto; }
.panel-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.version { opacity: 0.65; font-size: 0.85em; font-variant-numeric: tabular-nums; }
.block { display: grid; gap: 6px; }
.block h3 { margin: 0; font-size: 0.95em; opacity: 0.85; }
.resources, .effects, .others, .reasons, .costs, .trace { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.resources li, .others li { display: flex; justify-content: space-between; gap: 8px; }
.value { font-variant-numeric: tabular-nums; }
.base { opacity: 0.6; text-decoration: line-through; }
.max { opacity: 0.6; }
.effects li { display: grid; grid-template-columns: 1fr auto auto; gap: 4px 8px; padding: 4px 0; }
.effects .source, .effects .duration { opacity: 0.7; font-size: 0.85em; }
.effects .changes { grid-column: 1 / -1; opacity: 0.8; font-size: 0.85em; font-variant-numeric: tabular-nums; }
.checks { display: flex; flex-wrap: wrap; gap: 8px; }
.checks button { display: grid; justify-items: center; gap: 2px; padding: 8px 12px; cursor: pointer; }
.checks small { opacity: 0.65; }
.adjust { display: flex; gap: 6px; }
.result .headline { display: flex; gap: 10px; align-items: baseline; margin: 0; }
.degree { font-weight: 600; }
.dice { font-variant-numeric: tabular-nums; opacity: 0.75; }
.goal { display: flex; gap: 10px; margin: 0; }
.goal .ok { color: #9ece6a; }
.goal .bad { color: #f7768e; }
.goal .extent, .goal .severity { opacity: 0.75; font-variant-numeric: tabular-nums; }
.costs { font-variant-numeric: tabular-nums; opacity: 0.85; }
.trace-toggle { justify-self: start; }
.trace li { display: grid; grid-template-columns: auto auto 1fr; gap: 8px; align-items: baseline; }
.trace code { font-size: 0.85em; }
.trace .kind { opacity: 0.6; font-size: 0.8em; }
.trace .reason { opacity: 0.9; font-size: 0.9em; }
.hint { opacity: 0.75; font-size: 0.9em; margin: 0; }
.notice { color: #7aa2f7; margin: 0; font-size: 0.9em; }
</style>
