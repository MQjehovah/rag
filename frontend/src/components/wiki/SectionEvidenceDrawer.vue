<template>
  <el-drawer
    v-model="visible"
    class="section-evidence-drawer"
    title="章节证据"
    size="520px"
    direction="rtl"
    @closed="onClosed"
  >
    <div class="sev-body">
      <!-- 历史/非当前修订统一提示：正文为当前 Evidence 内容，非发布时原文（契约 §1 第 55 行） -->
      <div v-if="readingNonCurrent" class="sev-revision-note">
        当前查看为历史或草稿修订：以下展示的是当前 Evidence 内容，非发布时原文。
      </div>

      <div v-if="loading" v-loading="true" class="sev-state">正在加载章节证据…</div>

      <!-- 失败与空列表在 UI 中分开（契约 §4） -->
      <div v-else-if="error" class="sev-state sev-error">
        <div class="sev-error-title">章节证据加载失败</div>
        <div class="sev-error-msg">{{ error }}</div>
        <el-button size="small" type="primary" class="sev-retry" @click="load">重试</el-button>
      </div>

      <div v-else-if="items.length === 0" class="sev-state">暂无可查看的章节证据</div>

      <div v-else class="sev-list">
        <div v-for="ev in items" :key="ev.evidence_id" class="sev-item">
          <div class="sev-meta">
            <el-tag size="small" :type="typeTag(ev.evidence_type)">{{ ev.evidence_type || 'text' }}</el-tag>
            <el-tag v-if="ev.state === 'stale' || ev.status === 'stale'" size="small" type="warning">已过期</el-tag>
            <el-tag v-if="ev.state === 'rejected' || ev.status === 'rejected'" size="small" type="danger">已否决</el-tag>
            <el-tag v-if="ev.state === 'changed'" size="small" type="warning">内容已变化</el-tag>
            <el-tag v-if="ev.source_display_name" size="small" type="info" effect="plain" class="sev-source">
              {{ ev.source_display_name }}
            </el-tag>
            <span v-if="locatorParts(ev).length" class="sev-locators">
              <span v-for="(part, i) in locatorParts(ev)" :key="i" class="sev-locator-chip">{{ part }}</span>
            </span>
          </div>

          <!-- 绑定聚合：support/conflict 中文标签，冲突不判对错 -->
          <div v-if="ev.bindings && ev.bindings.length" class="sev-bindings">
            <span
              v-for="(b, i) in ev.bindings"
              :key="i"
              class="sev-binding"
              :class="{ 'sev-binding-conflict': b.usage_type === 'conflict' }"
            >
              {{ b.usage_type === 'conflict' ? '冲突' : '支持' }}
              <span v-if="b.field_path" class="sev-field">{{ b.field_path }}</span>
            </span>
          </div>

          <!-- 原文（已授权 EvidenceItem.content）：Markdown 安全渲染，长内容区域内部滚动 -->
          <div class="sev-content-scroll">
            <MarkdownPreview :content="contentText(ev)" />
          </div>
          <div v-if="ev.content_truncated" class="sev-truncated">内容较长，已截断</div>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { WikiSectionEvidenceItem } from '../../api/wiki'
import { wikiApi } from '../../api/wiki'
import MarkdownPreview from '../MarkdownPreview.vue'

const props = defineProps<{
  wikiId: string
  revisionId: string | null
  sectionId: string
  visible: boolean
  /** 是否在阅读历史/草稿修订（非 current_revision_id）——统一提示 Evidence 非发布时原文。 */
  readingNonCurrent?: boolean
}>()

const emit = defineEmits<{ (e: 'update:visible', v: boolean): void }>()

const visible = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

const loading = ref(false)
const error = ref('')
const items = ref<WikiSectionEvidenceItem[]>([])
let seq = 0
let alive = true

function keyReady(): boolean {
  return Boolean(props.wikiId && props.revisionId && props.sectionId)
}

async function load() {
  if (!keyReady()) return
  const token = ++seq
  loading.value = true
  error.value = ''
  items.value = []
  try {
    const data = await wikiApi.sectionEvidence({
      wikiId: props.wikiId,
      revisionId: props.revisionId as string,
      sectionId: props.sectionId,
      limit: 50,
      offset: 0,
    })
    if (!alive || token !== seq) return
    items.value = data.items || []
  } catch (e: any) {
    if (!alive || token !== seq) return
    error.value = e?.response?.data?.detail || '网络异常，章节证据加载失败'
  } finally {
    if (alive && token === seq) loading.value = false
  }
}

watch(
  () => [props.visible, props.wikiId, props.revisionId, props.sectionId],
  () => {
    if (props.visible && keyReady()) {
      load()
    } else {
      // 关闭/切上下文：递增序号使在途请求失效，避免旧内容被写入
      seq++
    }
  },
  { immediate: true },
)

function onClosed() {
  items.value = []
  error.value = ''
}

onBeforeUnmount(() => {
  alive = false
  seq++
})

function typeTag(type: string): 'primary' | 'success' | 'warning' | 'info' {
  if (type === 'table') return 'success'
  if (type === 'image') return 'warning'
  if (type === 'text') return 'primary'
  return 'info'
}

// 受限 locator 白名单展示（page_number/heading/image_id/content_type）
function locatorParts(ev: WikiSectionEvidenceItem): string[] {
  const out: string[] = []
  const l = ev.locator || {}
  if (l.page_number != null) out.push(`第 ${l.page_number} 页`)
  if (l.heading) out.push(String(l.heading))
  if (l.image_id) out.push(`图片 ${l.image_id}`)
  if (l.content_type) out.push(String(l.content_type))
  return out
}

function contentText(ev: WikiSectionEvidenceItem): string {
  return typeof ev.content === 'string' ? ev.content : ''
}
</script>

<style scoped>
.sev-body {
  min-height: 180px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.sev-revision-note {
  font-size: 12px;
  color: #92400e;
  background: #fffbeb;
  border: 1px solid #f59e0b;
  border-radius: 6px;
  padding: 6px 10px;
}
.sev-state {
  text-align: center;
  color: #9ca3af;
  padding: 40px 12px;
  font-size: 13px;
  min-height: 120px;
}
.sev-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  color: #b91c1c;
}
.sev-error-title {
  font-weight: 600;
}
.sev-error-msg {
  font-size: 12px;
  color: #6b7280;
  max-width: 90%;
}
.sev-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.sev-item {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 12px 14px;
  background: #fff;
}
.sev-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 8px;
}
.sev-source {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sev-locators {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.sev-locator-chip {
  font-size: 12px;
  color: #374151;
  background: #f3f4f6;
  padding: 1px 8px;
  border-radius: 6px;
}
.sev-bindings {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.sev-binding {
  font-size: 12px;
  color: #166534;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 999px;
  padding: 1px 8px;
}
.sev-binding-conflict {
  color: #b45309;
  background: #fffbeb;
  border-color: #fde68a;
}
.sev-field {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  margin-left: 4px;
}
.sev-content-scroll {
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid #f3f4f6;
  border-radius: 8px;
  padding: 6px 10px;
  background: #fafafa;
}
.sev-truncated {
  margin-top: 6px;
  font-size: 12px;
  color: #b45309;
}
</style>
