import { defineAsyncComponent, type Component } from 'vue'

type ExperienceLoader = () => Promise<{ default: Component }>
type ComponentLoader = () => Promise<{ default: Component }>

export type RulesetPlayTool = 'campaign' | 'combat' | 'rules'

export interface RulesetPlayCopy {
  menu: string
  campaign: string
  combat: string
  rules: string
  title: string
}

export interface RulesetPlayExtension {
  campaign: Component | null
  combat: Component | null
  rules: Component | null
  copy: (language: string) => RulesetPlayCopy
}

export interface RulesetAdvancementExtension {
  component: Component
  title: (language: string) => string
}

const loaders: Record<string, ExperienceLoader> = {
  dnd2024: () => import('./dnd2024/create/Dnd2024CharacterBuilder.vue'),
  // 声明式规则（profile="custom"）的建卡器。少了这一条，入局/建房页会直接报
  // `Unsupported ruleset experience: custom`。
  custom: () => import('./custom/CustomRulesetCharacterBuilder.vue'),
}
const components = new Map<string, Component>()
const playLoaders: Record<string, Partial<Record<RulesetPlayTool, ComponentLoader>>> = {
  'core:dnd2024': {
    campaign: () => import('./dnd2024/campaign/Dnd2024CampaignPanel.vue'),
    combat: () => import('./dnd2024/combat/Dnd2024CombatPanel.vue'),
  },
  // 声明式规则没有战役/战斗循环，只有“规则与检定”。
  'custom:declarative': {
    rules: () => import('./custom/CustomRulesetPanel.vue'),
  },
}
const playComponents = new Map<string, Component>()
const advancementLoaders: Record<string, ComponentLoader> = {
  'core:dnd2024': () => import('./dnd2024/progression/Dnd2024AdvancementPanel.vue'),
}
const advancementComponents = new Map<string, Component>()
const characterCenterLoaders: Record<string, ComponentLoader> = {
  'core:dnd2024': () => import('./ProfessionalCharacterCenter.vue'),
}
const characterCenterComponents = new Map<string, Component>()

function resolvePlayComponent(runtimeId: string, tool: RulesetPlayTool): Component | null {
  const loader = playLoaders[runtimeId]?.[tool]
  if (!loader) return null
  const key = `${runtimeId}:${tool}`
  const cached = playComponents.get(key)
  if (cached) return cached
  const component = defineAsyncComponent(loader)
  playComponents.set(key, component)
  return component
}

export function resolveRulesetExperience(profile: string): Component | null {
  const loader = loaders[profile]
  if (!loader) return null
  const cached = components.get(profile)
  if (cached) return cached
  const component = defineAsyncComponent(loader)
  components.set(profile, component)
  return component
}

const playCopies: Record<string, (language: string) => RulesetPlayCopy> = {
  'core:dnd2024': language => language.startsWith('zh') ? {
    menu: 'DND5E工具', campaign: '冒险与战役', combat: '战斗工具',
    rules: 'DND5E工具', title: 'DND5E工具',
  } : {
    menu: 'DND5E Tools', campaign: 'Adventure & campaign', combat: 'Combat tools',
    rules: 'DND5E Tools', title: 'DND5E Tools',
  },
  'custom:declarative': language => language.startsWith('zh') ? {
    menu: '规则与检定', campaign: '冒险与战役', combat: '战斗工具',
    rules: '规则与检定', title: '规则与检定',
  } : {
    menu: 'Rules & checks', campaign: 'Adventure & campaign', combat: 'Combat tools',
    rules: 'Rules & checks', title: 'Rules & checks',
  },
}

const DEFAULT_PLAY_COPY = (language: string): RulesetPlayCopy => language.startsWith('zh') ? {
  menu: '规则工具', campaign: '冒险与战役', combat: '战斗工具', rules: '规则与检定',
  title: '规则工具',
} : {
  menu: 'Ruleset tools', campaign: 'Adventure & campaign', combat: 'Combat tools',
  rules: 'Rules & checks', title: 'Ruleset tools',
}

export function resolveRulesetPlayExtension(runtimeId: string): RulesetPlayExtension | null {
  if (!playLoaders[runtimeId]) return null
  return {
    campaign: resolvePlayComponent(runtimeId, 'campaign'),
    combat: resolvePlayComponent(runtimeId, 'combat'),
    rules: resolvePlayComponent(runtimeId, 'rules'),
    copy: playCopies[runtimeId] || DEFAULT_PLAY_COPY,
  }
}

export function resolveRulesetAdvancementExtension(
  runtimeId: string,
): RulesetAdvancementExtension | null {
  const loader = advancementLoaders[runtimeId]
  if (!loader) return null
  let component = advancementComponents.get(runtimeId)
  if (!component) {
    component = defineAsyncComponent(loader)
    advancementComponents.set(runtimeId, component)
  }
  return {
    component,
    title: language => language.startsWith('zh')
      ? 'D&D 2024 职业升级'
      : 'D&D 2024 advancement',
  }
}

export function resolveRulesetCharacterCenter(runtimeId: string): Component | null {
  const loader = characterCenterLoaders[runtimeId]
  if (!loader) return null
  const cached = characterCenterComponents.get(runtimeId)
  if (cached) return cached
  const component = defineAsyncComponent(loader)
  characterCenterComponents.set(runtimeId, component)
  return component
}
