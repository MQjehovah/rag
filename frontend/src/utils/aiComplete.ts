/** AI 行内补全(ghost text)的纯逻辑: 前后文摘取与裁剪、触发条件、请求序号/失败冷却、Tab 采纳判定。 */

/** 光标前文上限(与后端 COMPLETE_BEFORE_MAX 一致) */
export const COMPLETE_BEFORE_MAX = 2000
/** 光标后文上限(与后端 COMPLETE_AFTER_MAX 一致) */
export const COMPLETE_AFTER_MAX = 1000
/** 停止输入后触发补全的静默时长 */
export const COMPLETE_DEBOUNCE_MS = 1200
/** 同一位置失败的静默冷却时长 */
export const COMPLETE_FAIL_COOLDOWN_MS = 30000

export interface CompletionGate {
  enabled: boolean
  focused: boolean
  emptySelection: boolean
  inCodeBlock: boolean
  inTable: boolean
}

export interface CompletionSuggestion {
  pos: number
  text: string
}

export interface CompletionFailure {
  pos: number
  at: number
}

/** 静态触发条件(纯函数): 开关开、编辑器聚焦、空选区、不在代码块/表格内。 */
export function canTriggerCompletion(gate: CompletionGate): boolean {
  return gate.enabled && gate.focused && gate.emptySelection && !gate.inCodeBlock && !gate.inTable
}

/** 摘取区间(纯函数): 光标向前/向后各取 max 字符对应的文档位置。 */
export function completionRanges(
  docSize: number,
  pos: number,
  beforeMax = COMPLETE_BEFORE_MAX,
  afterMax = COMPLETE_AFTER_MAX,
): { beforeFrom: number; afterTo: number } {
  return {
    beforeFrom: Math.max(0, pos - beforeMax),
    afterTo: Math.min(docSize, pos + afterMax),
  }
}

/** 前文裁剪(纯函数): textBetween 因块分隔符可能超出上限, 仅保留末尾 ≤max 字符。 */
export function clipBefore(text: string, max = COMPLETE_BEFORE_MAX): string {
  return text.length > max ? text.slice(text.length - max) : text
}

/** 后文裁剪(纯函数): 仅保留开头 ≤max 字符。 */
export function clipAfter(text: string, max = COMPLETE_AFTER_MAX): string {
  return text.length > max ? text.slice(0, max) : text
}

/** 请求序号是否仍是最新(纯函数): 旧序号的响应必须丢弃, 防竞态。 */
export function isCurrentSeq(seq: number, latest: number): boolean {
  return seq === latest
}

/** 同一位置是否仍在失败冷却期内(纯函数)。 */
export function inFailCooldown(
  failure: CompletionFailure | null | undefined,
  pos: number,
  now: number,
): boolean {
  if (!failure) return false
  return failure.pos === pos && now - failure.at < COMPLETE_FAIL_COOLDOWN_MS
}

/** 建议是否仍可展示/采纳(纯函数): 有建议、空选区、光标仍在建议位置。 */
export function canAcceptSuggestion(
  suggestion: CompletionSuggestion | null | undefined,
  head: number,
  emptySelection: boolean,
): boolean {
  return !!suggestion && emptySelection && suggestion.pos === head
}
