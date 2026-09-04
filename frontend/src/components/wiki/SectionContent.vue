<template>
  <div class="section-dispatch">
    <!-- Phase 8B：仅 endpoint role + 合法 display（运行时契约校验通过）时结构化展示；
         历史/未知/非法 display 一律回退 Markdown 原文，整节降级，不做部分渲染。 -->
    <ApiEndpointSection
      v-if="structured"
      :section-id="section.id"
      :display="structured"
    />
    <MarkdownPreview v-else :content="section.content" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ApiSectionDisplay, WikiSection } from '../../api/wiki'
import MarkdownPreview from '../MarkdownPreview.vue'
import ApiEndpointSection from './ApiEndpointSection.vue'

const props = defineProps<{
  section: WikiSection
}>()

// 只读展示契约的展示版本（与后端 DISPLAY_CONTRACT.md / wiki.ts 一致）。
const DISPLAY_SCHEMA = 'api-section-display/v2'
const MEDIA_STATUS = new Set(['present', 'unspecified'])
const PARAM_LOCATIONS = new Set(['path', 'query', 'header'])

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}
function isStr(v: unknown): v is string {
  return typeof v === 'string'
}
function isRealBool(v: unknown): v is boolean {
  return typeof v === 'boolean'
}
function isStrArray(v: unknown): v is unknown[] {
  return Array.isArray(v)
}

function validMediaItem(v: unknown): boolean {
  if (!isRecord(v)) return false
  return isStr(v.media_type) && isStr(v.schema_status) && MEDIA_STATUS.has(v.schema_status)
}
function validMediaList(v: unknown): boolean {
  if (!isStrArray(v)) return false
  return v.every(validMediaItem)
}
function validParam(v: unknown): boolean {
  if (!isRecord(v)) return false
  return isStr(v.location) && PARAM_LOCATIONS.has(v.location)
    && isStr(v.name) && isRealBool(v.required) && isStr(v.description) && isStr(v.type)
}
function validStrPairs(v: unknown, keys: string[]): boolean {
  if (!isStrArray(v)) return false
  return v.every((item) => isRecord(item) && keys.every((k) => isStr(item[k])))
}

/**
 * 轻量展示契约校验（不依赖 TS 类型）：任一必需字段缺失/类型不符/版本不符 → 整节回退
 * Markdown。不解析 Markdown、不调模型补结构。所有检查在 try/catch 内，异常视为不合法。
 */
function sectionDisplayUsable(d: unknown): d is ApiSectionDisplay {
  try {
    if (!isRecord(d)) return false
    if (d.schema_version !== DISPLAY_SCHEMA) return false
    if (d.section_role !== 'endpoint') return false
    if (!isStr(d.content_hash) || !isStr(d.version_scope)) return false

    const ep = d.endpoint
    if (!isRecord(ep)) return false
    if (!isStr(ep.method) || !isStr(ep.path)) return false
    if (!isStr(ep.summary) || !isStr(ep.description)) return false

    if (!isStrArray(d.parameters) || !d.parameters.every(validParam)) return false
    if (!isStrArray(d.responses)) return false
    for (const r of d.responses) {
      if (!isRecord(r)) return false
      if (!isStr(r.status_code) || !isStr(r.description)) return false
      if (!validMediaList(r.media_types)) return false
    }
    if (d.request_body !== null) {
      if (!isRecord(d.request_body)) return false
      if (!isRealBool(d.request_body.required) || !isStr(d.request_body.description)) return false
      if (!validMediaList(d.request_body.media_types)) return false
    }
    if (!isStrArray(d.error_codes)) return false
    for (const e of d.error_codes) {
      if (!isRecord(e)) return false
      if (!isStr(e.code) || !isStr(e.description) || !isStr(e.http_status)) return false
    }
    if (!isStrArray(d.examples)) return false
    for (const ex of d.examples) {
      if (!isRecord(ex)) return false
      if (!isStr(ex.title) || !isStr(ex.description) || !isStr(ex.media_type)) return false
      if (!('content' in ex)) return false
    }
    if (!validStrPairs(d.version_notes, ['version_scope', 'note'])) return false
    if (!validStrPairs(d.knowledge_gaps, ['gap_type', 'description'])) return false
    if (!validStrPairs(d.conflicts, ['field_path'])) return false
    return true
  } catch {
    return false
  }
}

const structured = computed<ApiSectionDisplay | null>(() => {
  const sec = props.section
  if (sec.section_role !== 'endpoint') return null
  if (!sec.display) return null
  return sectionDisplayUsable(sec.display) ? sec.display : null
})
</script>
