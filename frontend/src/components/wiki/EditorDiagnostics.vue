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
        {{ viewingRevisionLabel }}
      </span>
      <span class="ed-arrow">{{ expanded ? '收起' : '展开' }}</span>
    </button>

    <div v-show="expanded" class="ed-body">
      <div v-if="loading" v-loading="true" class="ed-state">正在加载诊断…</div>

      <div v-else-if="error" class="ed-state ed-error">
        <template v-if="errorKind === 'forbidden-view'">
          <span class="ed-error-title">无权查看该版本</span>
          <span class="ed-error-msg">{{ error }}</span>
        </template>
        <template v-else>
          <span>诊断加载失败：{{ error }}</span>
        </template>
        <el-button size="small" type="primary" class="ed-retry" @click="load">重试</el-button>
      </div>

      <template v-else-if="data">
        <div v-if="data.revision_id" class="ed-revision-note">查看修订：{{ shortId(data.revision_id) }}</div>

        <div class="ed-block">
          <div class="ed-block-title">Skill 配置</div>
          <template v-if="data.skill">
            <div class="ed-line">
              <span class="ed-label">使用 Skill：</span>
              <span class="ed-skill-name">{{ data.skill.display_name || data.skill.key || '未命名' }}</span>
              <el-tag v-if="data.skill.version" size="small">{{ data.skill.version }}</el-tag>
              <el-tag v-if="data.skill.key" size="small" type="info" effect="plain">{{ data.skill.key }}</el-tag>
              <el-tag v-if="data.is_current_wiki_config" size="small" type="success">当前 Wiki 配置</el-tag>
              <el-tag v-else size="small" type="info">历史配置</el-tag>
            </div>
            <div class="ed-line">
              <span class="ed-label">选择方式：</span>
              <el-tag size="small" type="info" effect="plain" class="ed-selected-by">{{ selectedByLabel(data.skill.selected_by) }}</el-tag>
              <span class="ed-sep" />
              <span class="ed-label">锁定状态：</span>
              <el-tag :type="data.skill.locked ? 'warning' : 'success'" size="small" class="ed-locked">
                {{ data.skill.locked ? '已锁定' : '未锁定' }}
              </el-tag>
            </div>
            <div class="ed-line ed-reason">
              <span class="ed-label">判定原因：</span>
              <span class="ed-reason-text">{{ reasonLabel(data.skill.reason_code) }}</span>
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
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import type { WikiDiagnostics, WikiSkillSelectedBy } from '../../api/wiki'
import { wikiApi } from '../../api/wiki'

const props = defineProps<{
  wikiId: string
  revisionId: string | null | undefined
}>()

const expanded = ref(true)
const loading = ref(false)
const error = ref('')
/** 404（Revision 不存在或无权查看该版本）与其它失败分开提示。 */
const errorKind = ref<'forbidden-view' | 'other'>('other')
const data = ref<WikiDiagnostics | null>(null)
let seq = 0
let alive = true

const viewingRevisionLabel = computed(() => {
  const rev = props.revisionId
  if (!rev) return '当前修订'
  return `修订 ${shortId(rev)}`
})

function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 12)}…` : id
}

async function load() {
  if (!props.wikiId) return
  const token = ++seq
  loading.value = true
  error.value = ''
  errorKind.value = 'other'
  data.value = null
  try {
    const res = await wikiApi.wikiDiagnostics(props.wikiId, props.revisionId || undefined)
    if (!alive || token !== seq) return
    data.value = res
  } catch (e: any) {
    if (!alive || token !== seq) return
    const status = e?.response?.status
    if (status === 404) {
      errorKind.value = 'forbidden-view'
      error.value = 'Revision 不存在或当前账号无权查看该版本'
    } else {
      error.value = e?.response?.data?.detail || '网络异常，诊断加载失败'
    }
  } finally {
    if (alive && token === seq) loading.value = false
  }
}

// 切换 wiki / 查看 revision：重新请求；旧响应由 ctxGen(seq) 失效，卸载即失效。
watch(
  () => [props.wikiId, props.revisionId],
  () => {
    if (props.wikiId && props.revisionId !== undefined) {
      void load()
    } else {
      seq++
      data.value = null
      error.value = ''
      loading.value = false
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  alive = false
  seq++
})

// selected_by 受限枚举 → 中文“选择方式”标签（不是操作者姓名）。
const SELECTED_BY_LABELS: Record<WikiSkillSelectedBy, string> = {
  auto: '自动',
  manual: '人工',
  migration: '迁移',
  default_fallback: '默认回退',
  locked: '锁定',
  sticky: '沿用上一技能',
}

function selectedByLabel(v: WikiSkillSelectedBy | null | undefined): string {
  if (!v) return '未记录'
  return SELECTED_BY_LABELS[v] || v
}

// reason_code 受控映射文案；LLM_* 统一“自动判定”；unknown/其余 → 通用提示（契约 §2）。
const REASON_LABELS: Record<string, string> = {
  DETERMINISTIC_HIGH_CONFIDENCE: '确定性高置信自动判定',
  SKILL_STICKY_CURRENT: '沿用当前技能',
  SKILL_STICKY_MARGIN: '沿用当前技能',
  SKILL_LOCKED: '技能被锁定，强制沿用',
  LOCKED_SKILL_MISSING: '锁定技能缺失，退回默认',
  MANUAL_OVERRIDE: '人工指定并覆盖系统决策',
  MANUAL_UNLOCK: '人工解锁后指定',
  MIGRATION_PROPOSED: '已提议迁移技能',
  MIGRATION_APPLIED: '已应用技能迁移',
  SKILL_MATCH_FALLBACK: '技能匹配失败，使用默认回退',
  NO_LLM_ROUTER: '未启用自动路由，采用确定规则',
  NO_CANDIDATES: '无候选技能',
  NO_DEFAULT_AVAILABLE: '无可用默认技能',
  ONLY_DEFAULT_AVAILABLE: '仅默认技能可用',
  SKILL_NOT_APPLICABLE: '技能不适用',
  SKILL_ROUTE_ERROR: '技能路由异常',
  unknown: '决策原因未知',
}

function reasonLabel(code: string | null | undefined): string {
  if (!code || code === 'unknown') return '决策原因未知'
  if (REASON_LABELS[code]) return REASON_LABELS[code]
  if (code.startsWith('LLM_') || code.startsWith('LOW_LLM_')) return '自动判定'
  return '该原因由系统内部管理，暂不对外展示'
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
.ed-error-title {
  font-weight: 700;
  font-size: 14px;
}
.ed-error-msg {
  font-size: 12px;
  color: #6b7280;
  max-width: 90%;
  overflow-wrap: anywhere;
}
.ed-revision-note {
  font-size: 12px;
  color: #6b7280;
  background: #f3f4f6;
  border-radius: 6px;
  padding: 4px 10px;
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
.ed-sep {
  width: 1px;
  height: 14px;
  background: #e5e7eb;
  margin: 0 4px;
}
.ed-skill-name {
  font-weight: 600;
  color: #111827;
}
.ed-reason {
  color: #6b7280;
}
.ed-reason-text {
  overflow-wrap: anywhere;
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
