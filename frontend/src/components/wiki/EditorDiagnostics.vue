<template>
  <section class="editor-diagnostics">
    <button
      type="button"
      class="ed-toggle"
      :aria-expanded="expanded ? 'true' : 'false'"
      @click="expanded = !expanded"
      @keydown.enter.prevent="expanded = !expanded"
      @keydown.space.prevent="expanded = !expanded"
    >
      <span class="ed-title">编译诊断</span>
      <span class="ed-sub">
        {{ props.revisionId ? `修订 ${props.revisionId}` : '当前修订' }}
      </span>
      <span class="ed-arrow">{{ expanded ? '收起' : '展开' }}</span>
    </button>

    <div v-show="expanded" class="ed-body">
      <div v-if="loading" v-loading="true" class="ed-state">正在加载诊断…</div>

      <div v-else-if="error" class="ed-state ed-error">
        <span>诊断加载失败：{{ error }}</span>
        <el-button size="small" type="primary" class="ed-retry" @click="load">重试</el-button>
      </div>

      <template v-else-if="data">
        <div class="ed-block">
          <div class="ed-block-title">Skill 配置</div>
          <template v-if="data.skill">
            <div class="ed-line">
              <span class="ed-label">使用 Skill：</span>
              <span class="ed-skill-name">{{ data.skill.display_name || data.skill.key }}</span>
              <el-tag v-if="data.skill.version" size="small">{{ data.skill.version }}</el-tag>
              <el-tag v-if="data.skill.key" size="small" type="info" effect="plain">{{ data.skill.key }}</el-tag>
              <el-tag v-if="data.is_current_wiki_config" size="small" type="success">当前 Wiki 配置</el-tag>
              <el-tag v-else size="small" type="info">历史配置</el-tag>
            </div>
            <div class="ed-line">
              <span class="ed-label">选择：</span>
              <span>{{ selectionLabel(data.skill.selection) }}</span>
              <el-tag v-if="data.skill.selected_by" size="small" type="info" effect="plain">
                由 {{ data.skill.selected_by }} 选定
              </el-tag>
              <el-tag :type="data.skill.locked ? 'warning' : 'success'" size="small">
                {{ data.skill.locked ? '已锁定' : '未锁定' }}
              </el-tag>
            </div>
            <div class="ed-line ed-reason">
              <span class="ed-label">原因：</span>
              <span>{{ reasonText(data.skill.reason_code) }}</span>
            </div>
          </template>
          <div v-else class="ed-hint">未提供 Skill 配置信息</div>
        </div>

        <div class="ed-block">
          <div class="ed-block-title">
            校验结果
            <el-tag v-if="data.validation" size="small" :type="summaryTag(data.validation.summary)">
              {{ summaryLabel(data.validation.summary) }}
            </el-tag>
          </div>
          <template v-if="data.validation && data.validation.sections && data.validation.sections.length">
            <div
              v-for="(s, i) in data.validation.sections"
              :key="i"
              class="ed-section-row"
            >
              <span class="ed-section-heading">{{ s.heading || '（未命名章节）' }}</span>
              <el-tag size="small" :type="sectionTag(s.validation_status)">
                {{ sectionLabel(s.validation_status) }}
              </el-tag>
            </div>
          </template>
          <div v-else class="ed-hint">当前修订无章节校验信息</div>
        </div>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import type { WikiDiagnostics } from '../../api/wiki'
import { wikiApi } from '../../api/wiki'

const props = defineProps<{
  wikiId: string
  revisionId: string | null | undefined
}>()

const expanded = ref(true)
const loading = ref(false)
const error = ref('')
const data = ref<WikiDiagnostics | null>(null)
let seq = 0
let alive = true

async function load() {
  if (!props.wikiId) return
  const token = ++seq
  loading.value = true
  error.value = ''
  data.value = null
  try {
    const res = await wikiApi.wikiDiagnostics(props.wikiId)
    if (!alive || token !== seq) return
    data.value = res
  } catch (e: any) {
    if (!alive || token !== seq) return
    error.value = e?.response?.data?.detail || '网络异常，诊断加载失败'
  } finally {
    if (alive && token === seq) loading.value = false
  }
}

watch(() => props.wikiId, load, { immediate: true })

onBeforeUnmount(() => {
  alive = false
  seq++
})

function selectionLabel(sel: string | null | undefined): string {
  if (sel === 'auto') return '自动'
  if (sel === 'manual') return '人工'
  if (sel === 'none' || sel === '') return '未选择'
  return String(sel || '未选择')
}

// reason_code 仅受控集合显示映射文案；unknown/其余一律通用提示（契约 §2）。
const REASON_TEXTS: Record<string, string> = {
  auto_top_score: '依据最高相关度自动选择',
  auto_single: '仅一个候选 Skill，自动采用',
  manual_selected: '人工指定该 Skill',
  unknown: '决策原因未知',
}

function reasonText(code: string | null | undefined): string {
  if (!code) return '决策原因未知'
  return REASON_TEXTS[code] || '该原因由系统内部管理，暂不对外展示'
}

function summaryLabel(s: string | null | undefined): string {
  if (s === 'pass') return '通过'
  if (s === 'fail') return '失败'
  if (s === 'unknown' || s === null || s === undefined) return '未知'
  return String(s)
}

function summaryTag(s: string | null | undefined): 'success' | 'danger' | 'warning' {
  if (s === 'pass') return 'success'
  if (s === 'fail') return 'danger'
  return 'warning'
}

function sectionLabel(status: string | null | undefined): string {
  if (status === 'pass') return '通过'
  if (status === 'fail') return '失败'
  return '未知'
}

function sectionTag(status: string | null | undefined): 'success' | 'danger' | 'warning' | 'info' {
  if (status === 'pass') return 'success'
  if (status === 'fail') return 'danger'
  return 'warning'
}
</script>

<style scoped>
.editor-diagnostics {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fbfdff;
  overflow: hidden;
}
.ed-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  font: inherit;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: none;
  padding: 8px 12px;
  color: #1f2937;
}
.ed-toggle:focus-visible {
  outline: 2px solid #2563eb;
  outline-offset: -2px;
}
.ed-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
}
.ed-sub {
  font-size: 12px;
  color: #9ca3af;
  flex: 1;
}
.ed-arrow {
  font-size: 12px;
  color: #6b7280;
}
.ed-body {
  border-top: 1px solid #f3f4f6;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ed-state {
  text-align: center;
  color: #9ca3af;
  padding: 18px 8px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.ed-error {
  color: #b91c1c;
}
.ed-block {
  border: 1px solid #f3f4f6;
  border-radius: 8px;
  padding: 8px 10px;
  background: #fff;
}
.ed-block-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ed-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #4b5563;
  margin-top: 4px;
}
.ed-label {
  color: #9ca3af;
}
.ed-skill-name {
  font-weight: 600;
  color: #111827;
}
.ed-reason {
  color: #6b7280;
}
.ed-hint {
  font-size: 12px;
  color: #9ca3af;
}
.ed-section-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px dashed #f3f4f6;
  font-size: 12px;
}
.ed-section-row:last-child {
  border-bottom: none;
}
.ed-section-heading {
  color: #374151;
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
</style>
