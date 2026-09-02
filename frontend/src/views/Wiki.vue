<template>
  <div class="wiki-page">
    <div v-if="isAcceptance" class="acceptance-banner">
      ⚠️ 副本验收环境（数据为 wiki-acceptance.db 副本，非正式数据）
    </div>
    <header class="wiki-header">
      <div class="wiki-stats">{{ pages.length }} 个主题</div>
      <div class="wiki-controls">
        <el-input
          v-model="searchText"
          placeholder="搜索主题..."
          clearable
          style="width: 200px"
        />
        <el-select v-model="categoryFilter" placeholder="分类" clearable style="width: 140px" @change="load">
          <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
        </el-select>
        <el-select
          v-if="isAdmin"
          v-model="statusFilter"
          placeholder="状态"
          clearable
          style="width: 120px"
          @change="load"
        >
          <el-option label="预览" value="draft" />
          <el-option label="已发布" value="published" />
          <el-option label="已归档" value="archived" />
        </el-select>
        <el-button v-if="isAdmin" size="small" @click="refreshDirty">增量刷新</el-button>
        <el-button v-if="isAdmin" type="primary" @click="rebuild">重建主题</el-button>
      </div>
    </header>

    <div v-if="isAdmin && rebuildMsg" class="rebuild-banner">{{ rebuildMsg }}</div>

    <div class="wiki-body">
      <section class="catalog-pane">
        <h3 class="pane-title">主题目录</h3>
        <div class="catalog-scroll">
          <div v-for="group in groupedPages" :key="group.category" class="catalog-group">
            <div class="catalog-group-title">{{ group.category }}（{{ group.pages.length }}）</div>
            <div
              v-for="p in group.pages"
              :key="p.id"
              class="catalog-item"
              :class="{ active: p.id === currentPageId }"
              @click="openPage(p.id)"
            >
              <div class="catalog-title">{{ p.title }}</div>
              <el-tag :type="statusTag(p.status)" size="small">{{ statusLabel(p.status) }}</el-tag>
            </div>
          </div>
          <div v-if="!pages.length" class="empty-tip">{{ searchText || categoryFilter ? '无匹配主题' : '暂无已发布主题' }}</div>
        </div>
      </section>

      <section class="detail-pane">
        <div v-if="detailError" class="empty-tip">{{ detailError }}</div>
        <div v-else-if="!detail" class="empty-tip">选择左侧主题查看详情</div>
        <div v-else class="detail-content">
          <div class="breadcrumb">
            <span>{{ detail.category }}</span>
            <span v-if="searchText">搜索：{{ searchText }}</span>
          </div>
          <div class="detail-header">
            <h2 class="detail-title">{{ detail.title }}</h2>
            <el-tag :type="statusTag(detail.status)">{{ statusLabel(detail.status) }}</el-tag>
            <el-tag v-if="detail.locked" type="warning">已锁定</el-tag>
            <el-tag v-if="detail.latest_version" type="success">当前最新版本：{{ detail.latest_version }}</el-tag>
            <el-tag v-if="viewingPreview" type="warning">预览</el-tag>
          </div>

          <p class="detail-summary">{{ detail.summary }}</p>

          <div v-for="sec in detail.sections" :key="sec.id" class="section-item">
            <div class="section-head">
              <h4 class="section-heading">
                <span v-if="sec.heading?.startsWith('[dirty]')">🟡 </span>{{ sec.heading || sectionLabel(sec) }}
              </h4>
              <el-tag v-if="sec.is_common" size="small" type="info">通用说明</el-tag>
              <el-tag v-else-if="sec.version_label === 'unversioned'" size="small" type="info">版本未标明</el-tag>
              <el-tag v-else-if="sec.version_label" size="small" type="success">版本 {{ sec.version_label }}</el-tag>
              <el-tag v-if="sec.locked" size="small" type="warning">锁定</el-tag>
              <template v-if="canEdit">
                <el-button v-if="!editingId" size="small" text type="primary" @click="startEdit(sec)">编辑</el-button>
                <template v-if="viewingPreview">
                  <el-button v-if="!sec.locked" size="small" text type="warning" @click="lockSection(sec)">锁定</el-button>
                  <el-button v-else size="small" text type="info" @click="unlockSection(sec)">解锁</el-button>
                </template>
              </template>
            </div>
            <div v-if="sec.diff_notice" class="diff-notice">⚠️ {{ sec.diff_notice }}</div>

            <!-- 编辑态 -->
            <div v-if="editingId === sec.id" class="edit-box">
              <el-input v-model="editingContent" type="textarea" :rows="6" />
              <div class="edit-actions">
                <el-button size="small" type="primary" @click="saveSection(sec)">保存</el-button>
                <el-button size="small" @click="cancelEdit">取消</el-button>
                <span class="edit-hint">人工内容将在后续重建中保留（保存后自动锁定）。</span>
              </div>
            </div>
            <!-- 阅读态 -->
            <div v-else class="section-content">
              <MarkdownPreview :content="sec.content" />
            </div>

            <div v-if="sec.citations.length" class="section-citations">
              <span class="citations-label">引用：</span>
              <button
                v-for="c in sec.citations"
                :key="c.id"
                type="button"
                class="cite-chip"
                aria-haspopup="dialog"
                @click="openCitation(c)"
                @keydown.enter.prevent="openCitation(c)"
              >
                <template v-if="c.evidence">证据：{{ c.evidence.preview }}</template>
                <template v-else>来源</template>
              </button>
            </div>
          </div>

          <div v-if="detail.related_topics?.length" class="related-topics">
            <h4 class="section-heading">相关主题</h4>
            <div class="related-list">
              <div
                v-for="t in detail.related_topics"
                :key="t.id"
                class="related-item"
                @click="openPage(t.id)"
              >
                <span class="related-title">{{ t.title }}</span>
                <el-tag size="small" type="info">{{ t.category }}</el-tag>
                <span v-if="t.relation_reason.length" class="related-reason">{{ t.relation_reason.join('；') }}</span>
              </div>
            </div>
          </div>

          <footer v-if="canEdit" class="detail-actions">
            <el-button v-if="isAdmin && detail.status !== 'archived'" size="small" type="danger" @click="archivePage">归档</el-button>
            <el-button size="small" @click="openRevisions">版本历史</el-button>
          </footer>
        </div>
      </section>
    </div>

    <!-- Evidence Drawer -->
    <EvidenceDrawer
      v-model="evidenceVisible"
      :page-id="evidencePageId"
    />

    <!-- Revision 历史 Drawer（含 Diff） -->
    <el-drawer v-model="showRevisions" title="版本历史" size="520px">
      <div class="rev-list">
        <div v-for="rev in revisions" :key="rev.id" class="rev-item">
          <el-tag :type="statusTag(rev.status)" size="small">{{ statusLabel(rev.status) }}</el-tag>
          <span class="rev-meta">{{ rev.created_at?.slice(0, 10) }}</span>
          <el-button size="small" text type="primary" @click="viewDiff(rev.id)">对比</el-button>
          <el-button v-if="isAdmin && rev.status !== 'published'" size="small" text type="success" @click="publishRev(rev.id)">发布</el-button>
          <el-button v-if="canEdit" size="small" text @click="rollbackRev(rev.id)">回滚</el-button>
        </div>
        <div v-if="!revisions.length" class="empty-tip">无 Revision</div>
        <div v-if="diff" class="diff-box">
          <h4>变更对比</h4>
          <div v-for="s in diff.added" :key="'a' + s.section_type" class="diff-add">+ {{ s.heading }}：{{ s.new_content }}</div>
          <div v-for="s in diff.removed" :key="'r' + s.section_type" class="diff-remove">- {{ s.heading }}：{{ s.old_content }}</div>
          <div v-for="s in diff.changed" :key="'c' + s.section_type" class="diff-change">~ {{ s.heading }}：{{ s.old_content }} → {{ s.new_content }}</div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  wikiApi,
  type WikiCitation,
  type WikiDetail,
  type WikiDiff,
  type WikiPageSummary,
  type WikiRevision,
  type WikiSection,
} from '../api/wiki'
import { useAuthStore } from '../stores/auth'
import MarkdownPreview from '../components/MarkdownPreview.vue'
import EvidenceDrawer from '../components/EvidenceDrawer.vue'

const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()
const isAdmin = computed(() =>
  Boolean(authStore.user?.is_admin || authStore.user?.groups?.includes('__local_admin__')),
)
// V4 Phase B：wiki_editor 在有权访问的 Wiki 页面可见编辑/锁定/解锁/回滚入口。
const isWikiEditor = computed(() => Boolean(authStore.user?.is_wiki_editor))
const canEdit = computed(() => isAdmin.value || isWikiEditor.value)
// 副本验收环境标记（来自 .env.acceptance 的 VITE_ACCEPTANCE）
const isAcceptance = import.meta.env.VITE_ACCEPTANCE === 'true' || import.meta.env.MODE === 'acceptance'

const pages = ref<WikiPageSummary[]>([])
const statusFilter = ref<string | undefined>(undefined)
const searchText = ref('')
const categoryFilter = ref<string | undefined>(undefined)
const currentPageId = ref('')
const detail = ref<WikiDetail | null>(null)
const detailError = ref('')
const revisions = ref<WikiRevision[]>([])
const showRevisions = ref(false)
const viewingPreview = ref(false)
const diff = ref<WikiDiff | null>(null)

// 编辑
const editingId = ref('')
const editingContent = ref('')

// 引用追溯（J-2：仅 Evidence，不再有 Card 追溯）
const evidenceVisible = ref(false)
const evidencePageId = ref('')

// 重建进度
const rebuildMsg = ref('')
let rebuildTimer: ReturnType<typeof setInterval> | null = null
let searchDebounce: ReturnType<typeof setTimeout> | null = null

const STATUS_LABELS: Record<string, string> = {
  draft: '预览',
  pending: '预览',
  published: '已发布',
  rejected: '已归档',
  archived: '已归档',
  superseded: '已替代',
}

const SECTION_LABELS: Record<string, string> = {
  summary: '主题概览', scope: '适用范围', prerequisite: '前置条件', steps: '操作步骤',
  parameters: '参数与配置', rules: '规则与约束', warnings: '注意事项', diagnostics: '故障诊断',
  facts: '关键事实', entities: '关键实体', evidence: '证据来源', gaps: '知识缺口', related_topics: '相关主题',
}

function sectionLabel(t: string | WikiSection): string {
  if (typeof t === 'string') return SECTION_LABELS[t] || t
  if (t.is_common) return '通用说明'
  if (t.version_label === 'unversioned') return '版本未标明'
  if (t.version_label) return `版本 ${t.version_label}`
  return SECTION_LABELS[t.section_type] || t.section_type
}

function statusLabel(s: string): string {
  return STATUS_LABELS[s] || s
}

function statusTag(s: string): 'info' | 'warning' | 'success' | 'danger' {
  if (s === 'published') return 'success'
  if (s === 'draft' || s === 'pending') return 'warning'
  if (s === 'rejected' || s === 'superseded' || s === 'archived') return 'danger'
  return 'info'
}

const categories = computed(() => {
  const set = new Set<string>()
  pages.value.forEach((p) => { if (p.category) set.add(p.category) })
  return Array.from(set)
})

const groupedPages = computed(() => {
  const groups: Record<string, WikiPageSummary[]> = {}
  pages.value.forEach((p) => {
    const c = p.category || '综合主题'
    ;(groups[c] ||= []).push(p)
  })
  return Object.entries(groups).map(([category, list]) => ({ category, pages: list }))
})

async function load() {
  try {
    pages.value = await wikiApi.list({
      status: isAdmin.value ? statusFilter.value : 'published',
      q: searchText.value || undefined,
      category: categoryFilter.value,
    })
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载主题失败')
  }
}

function updateUrl() {
  if (currentPageId.value) {
    router.replace({ path: `/knowledge/wiki/${currentPageId.value}`, query: { q: searchText.value || undefined, category: categoryFilter.value || undefined } })
  } else {
    router.replace({ path: '/knowledge/wiki', query: { q: searchText.value || undefined, category: categoryFilter.value || undefined } })
  }
}

async function openPage(pageId: string, preview = false) {
  currentPageId.value = pageId
  viewingPreview.value = preview
  detailError.value = ''
  updateUrl()
  try {
    detail.value = await wikiApi.get(pageId, { preview })
  } catch (e: any) {
    detail.value = null
    detailError.value = e?.response?.status === 404 ? '该主题不存在或无权访问' : '主题内容加载失败'
  }
}

async function loadRevisions() {
  if (!currentPageId.value) return
  try {
    revisions.value = await wikiApi.revisions(currentPageId.value)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载版本历史失败')
  }
}

async function openRevisions() {
  showRevisions.value = true
  diff.value = null
  await loadRevisions()
}

async function viewDiff(revisionId: string) {
  if (!detail.value) return
  try {
    diff.value = await wikiApi.diff(detail.value.id, revisionId)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载 Diff 失败')
  }
}

function openCitation(c: WikiCitation) {
  if (c.evidence) {
    evidencePageId.value = c.evidence.page_id || ''
    evidenceVisible.value = true
  }
}

// 编辑/锁定
function startEdit(sec: WikiSection) {
  editingId.value = sec.id
  editingContent.value = sec.content
}

function cancelEdit() {
  editingId.value = ''
  editingContent.value = ''
}

async function saveSection(sec: WikiSection) {
  if (!detail.value) return
  const revisionId = detail.value.viewing_revision_id || detail.value.preview_revision_id
  if (!revisionId) return
  try {
    await wikiApi.updateSection(detail.value.id, revisionId, sec.id, editingContent.value)
    ElMessage.success('已保存')
    editingId.value = ''
    await openPage(detail.value.id, true)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

async function lockSection(sec: WikiSection) {
  if (!detail.value) return
  const revisionId = detail.value.viewing_revision_id || detail.value.preview_revision_id
  if (!revisionId) return
  try {
    await wikiApi.lockSection(detail.value.id, revisionId, sec.id)
    await openPage(detail.value.id, true)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '锁定失败')
  }
}

async function unlockSection(sec: WikiSection) {
  if (!detail.value) return
  const revisionId = detail.value.viewing_revision_id || detail.value.preview_revision_id
  if (!revisionId) return
  try {
    await wikiApi.unlockSection(detail.value.id, revisionId, sec.id)
    await openPage(detail.value.id, true)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '解锁失败')
  }
}

async function rebuild() {
  try {
    await wikiApi.rebuild()
    ElMessage.success('已启动重建')
    startPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重建失败')
  }
}

async function refreshDirty() {
  try {
    await wikiApi.refreshDirty()
    ElMessage.success('已提交后台刷新')
    startRefreshPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '增量刷新失败')
  }
}

let refreshTimer: ReturnType<typeof setInterval> | null = null

function startRefreshPolling() {
  stopRefreshPolling()
  refreshTimer = setInterval(async () => {
    try {
      const status = await wikiApi.refreshStatus()
      if (status.pending_pages === 0 && status.pending_wikis === 0 && status.backlog === 0) {
        stopRefreshPolling()
        await load()
        ElMessage.success('增量刷新完成')
      }
    } catch {
      // 请求失败或超时 → 停止轮询，不能永久运行
      stopRefreshPolling()
      ElMessage.error('刷新状态查询失败，已停止轮询')
    }
  }, 2500)
}

function stopRefreshPolling() {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

function startPolling() {
  stopPolling()
  rebuildTimer = setInterval(async () => {
    try {
      const status = await wikiApi.rebuildStatus()
      rebuildMsg.value = status.message
      if (!status.running) {
        stopPolling()
        await load()
        ElMessage.success('重建完成')
      }
    } catch {
      /* 忽略轮询错误 */
    }
  }, 2500)
}

function stopPolling() {
  if (rebuildTimer) {
    clearInterval(rebuildTimer)
    rebuildTimer = null
  }
}

async function archivePage() {
  if (!detail.value) return
  try {
    await wikiApi.archive(detail.value.id)
    ElMessage.success('已归档')
    detail.value = null
    currentPageId.value = ''
    updateUrl()
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '归档失败')
  }
}

async function publishRev(revisionId: string) {
  if (!detail.value) return
  try {
    await wikiApi.publish(detail.value.id, revisionId)
    ElMessage.success('已发布')
    viewingPreview.value = false
    await loadRevisions()
    await load()
    await openPage(detail.value.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '发布失败')
  }
}

async function rollbackRev(revisionId: string) {
  if (!detail.value) return
  try {
    await ElMessageBox.confirm(
      '回滚会把目标 Revision 重新发布，不会物理删除历史版本。确认回滚？',
      '回滚确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await wikiApi.rollback(detail.value.id, revisionId)
    ElMessage.success('已回滚')
    await loadRevisions()
    await openPage(detail.value.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '回滚失败')
  }
}

// 搜索 debounce
watch(searchText, () => {
  if (searchDebounce) clearTimeout(searchDebounce)
  searchDebounce = setTimeout(() => { load() }, 300)
})

watch(categoryFilter, () => { load() })

async function restoreFromRoute() {
  searchText.value = (route.query.q as string) || ''
  categoryFilter.value = (route.query.category as string) || undefined
  const pageId = route.params.pageId as string | undefined
  await load()
  if (pageId) {
    await openPage(pageId)
  }
}

onMounted(() => {
  restoreFromRoute()
  window.addEventListener('popstate', restoreFromRoute)
})

onBeforeUnmount(() => {
  stopPolling()
  stopRefreshPolling()
  if (searchDebounce) clearTimeout(searchDebounce)
  window.removeEventListener('popstate', restoreFromRoute)
})
</script>

<style scoped>
.wiki-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.acceptance-banner {
  background: #fef3c7;
  border: 1px solid #f59e0b;
  color: #92400e;
  font-size: 13px;
  padding: 6px 14px;
  border-radius: 6px;
}
.wiki-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.wiki-stats { font-size: 13px; color: #6b7280; }
.wiki-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.rebuild-banner { font-size: 13px; color: #b45309; background: #fffbeb; border: 1px solid #f59e0b; border-radius: 6px; padding: 6px 12px; }
.wiki-body {
  flex: 1;
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 16px;
  overflow: hidden;
}
.catalog-pane, .detail-pane {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.pane-title { font-size: 14px; font-weight: 600; color: #374151; margin-bottom: 8px; border-bottom: 1px solid #f3f4f6; padding-bottom: 6px; }
.catalog-scroll { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.catalog-group-title { font-size: 12px; color: #6b7280; font-weight: 600; margin: 4px 0 2px; }
.catalog-item {
  padding: 10px 12px;
  border: 1px solid #f3f4f6;
  border-radius: 6px;
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.catalog-item:hover { background: #f8fafc; }
.catalog-item.active { background: #eff6ff; border-color: #3b82f6; }
.catalog-title { font-size: 13px; color: #111827; font-weight: 500; }
.detail-content { overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }
.breadcrumb { display: flex; gap: 12px; font-size: 12px; color: #9ca3af; }
.detail-header { display: flex; align-items: center; gap: 10px; }
.detail-title { font-size: 18px; font-weight: 700; color: #111827; margin: 0; }
.detail-summary { font-size: 14px; color: #4b5563; background: #f8fafc; border-radius: 6px; padding: 10px 12px; margin: 0; }
.trace-box { font-size: 12px; color: #6b7280; background: #f9fafb; border-radius: 6px; padding: 8px 10px; }
.trace-line { margin-top: 4px; word-break: break-all; }
.section-item { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; }
.section-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.section-heading { font-size: 13px; font-weight: 600; color: #374151; margin: 0; flex: 1; }
.section-content { font-size: 13px; color: #4b5563; }
.edit-box { display: flex; flex-direction: column; gap: 8px; }
.edit-actions { display: flex; gap: 8px; align-items: center; }
.edit-hint { font-size: 12px; color: #9ca3af; }
.diff-notice {
  margin-top: 6px;
  font-size: 12px;
  color: #b45309;
  background: #fffbeb;
  border: 1px solid #f59e0b;
  border-radius: 6px;
  padding: 4px 8px;
}
.section-citations { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.citations-label { font-size: 12px; color: #9ca3af; }
.cite-chip {
  border: 1px solid #bbf7d0;
  background: #f0fdf4;
  color: #166534;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 12px;
  cursor: pointer;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cite-chip:hover { border-color: #16a34a; background: #dcfce7; }
.cite-chip:focus-visible {
  outline: 2px solid #2563eb;
  outline-offset: 2px;
}
.related-topics { margin-top: 4px; }
.related-list { display: flex; flex-direction: column; gap: 6px; }
.related-item { display: flex; gap: 8px; align-items: center; padding: 8px 10px; border: 1px solid #e5e7eb; border-radius: 6px; cursor: pointer; }
.related-item:hover { background: #f8fafc; }
.related-title { font-size: 13px; font-weight: 500; color: #111827; }
.related-reason { font-size: 12px; color: #9ca3af; flex: 1; }
.detail-actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; border-top: 1px solid #f3f4f6; padding-top: 12px; }
.rev-list { display: flex; flex-direction: column; gap: 8px; }
.rev-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid #f3f4f6; border-radius: 6px; }
.rev-meta { font-size: 12px; color: #6b7280; flex: 1; }
.diff-box { margin-top: 10px; border: 1px dashed #f59e0b; border-radius: 8px; padding: 10px; background: #fffbeb; display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.diff-add { color: #10b981; }
.diff-remove { color: #ef4444; }
.diff-change { color: #f59e0b; }
.empty-tip { font-size: 13px; color: #9ca3af; padding: 24px; text-align: center; }
</style>
