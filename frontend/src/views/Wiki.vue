<template>
  <div class="wiki-page">
    <div v-if="isAcceptance" class="acceptance-banner">
      ⚠️ 副本验收环境（数据为 wiki-acceptance.db 副本，非正式数据）
    </div>
    <header class="wiki-header">
      <div class="workspace-bar">
        <span class="ws-label">工作区</span>
        <WorkspaceSelector
          :model-value="workspaceId"
          :workspaces="browsableWorkspaces"
          :disabled="selectorDisabled"
          :loading="workspaceState === 'loading'"
          @update:model-value="onWorkspaceSelect"
        />
        <span v-if="currentWorkspace" class="ws-current-name">{{ currentWorkspace.name }}</span>
        <div v-if="workspaceState === 'ready'" class="wiki-stats">{{ pages.length }} 个主题</div>
      </div>

      <div v-if="workspaceState === 'ready'" class="wiki-controls">
        <el-input
          v-model="searchText"
          placeholder="搜索当前工作区..."
          clearable
          style="width: 200px"
        />
        <el-select v-model="categoryFilter" placeholder="分类" clearable style="width: 140px">
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
      </div>
    </header>

    <!-- 管理员：当前工作区绑定来源（只读） -->
    <WorkspaceNotebooks
      v-if="isAdmin && workspaceState === 'ready'"
      :workspace-id="workspaceId"
    />

    <!-- 管理员：全局管理（作用于全部工作区，与当前工作区无关） -->
    <div v-if="isAdmin && workspaceState !== 'loading'" class="global-manage">
      <div class="gm-title">全局管理</div>
      <div class="gm-note">以下操作作用于全部工作区，与当前所选工作区无关。</div>
      <div class="gm-actions">
        <el-button size="small" @click="refreshDirty">增量刷新（全部工作区）</el-button>
        <el-button size="small" type="primary" @click="rebuild">重建主题（全部工作区）</el-button>
      </div>
    </div>

    <div v-if="isAdmin && rebuildMsg" class="rebuild-banner">{{ rebuildMsg }}</div>

    <!-- 工作区级 UI 状态 -->
    <div v-if="workspaceState === 'loading'" v-loading="true" class="wiki-status">正在加载工作区…</div>
    <div v-else-if="workspaceState === 'error'" class="wiki-status status-error">
      <span>工作区加载失败：{{ workspaceLoadError }}</span>
      <el-button size="small" type="primary" @click="retryWorkspaces">重试</el-button>
    </div>
    <div v-else-if="workspaceState === 'none'" class="wiki-status">暂无可访问的 Wiki 工作区</div>
    <div v-else-if="workspaceState === 'inaccessible'" class="wiki-status status-error">
      当前工作区不可访问（不存在或无权访问）。可在上方选择其他工作区浏览。
    </div>

    <template v-else-if="workspaceState === 'ready'">
      <div class="wiki-body">
        <section class="catalog-pane" v-loading="listLoading">
          <h3 class="pane-title">主题目录</h3>
          <div class="catalog-scroll">
            <template v-if="!listError">
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
              <div v-if="!listLoading && !pages.length" class="empty-tip">{{ emptyCatalogTip }}</div>
            </template>
            <div v-else class="catalog-error">
              <span>{{ listError }}</span>
              <el-button size="small" type="primary" @click="load">重试</el-button>
            </div>
          </div>
        </section>

        <section class="detail-pane">
          <div v-if="detailError" class="detail-error">{{ detailError }}</div>
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
    </template>

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
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { isNavigationFailure, onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { LocationQueryRaw, LocationQueryValueRaw, RouteLocationNormalized } from 'vue-router'
import {
  wikiApi,
  type WikiCitation,
  type WikiDetail,
  type WikiDiff,
  type WikiPageSummary,
  type WikiRevision,
  type WikiSection,
} from '../api/wiki'
import { wikiWorkspacesApi, type WikiWorkspaceSummary } from '../api/wikiWorkspaces'
import { useAuthStore } from '../stores/auth'
import MarkdownPreview from '../components/MarkdownPreview.vue'
import EvidenceDrawer from '../components/EvidenceDrawer.vue'
import WorkspaceSelector from '../components/wiki/WorkspaceSelector.vue'
import WorkspaceNotebooks from '../components/wiki/WorkspaceNotebooks.vue'

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

// ===== 工作区浏览状态 =====
type WorkspaceState = 'loading' | 'ready' | 'none' | 'inaccessible' | 'error'

const workspaces = ref<WikiWorkspaceSummary[]>([])
const workspaceId = ref('')
const workspaceState = ref<WorkspaceState>('loading')
const workspaceLoadError = ref('')

// 只浏览 active 工作区（admin 列表中可能含 archived，不进入选择器）。
const browsableWorkspaces = computed(() =>
  workspaces.value
    .filter((w) => w.status === 'active')
    .sort((a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN')),
)
const currentWorkspace = computed(
  () => workspaces.value.find((w) => w.id === workspaceId.value) || null,
)
const workspaceReady = computed(() => workspaceState.value === 'ready' && workspaceId.value !== '')
// loading / error 时选择器不可用；inaccessible（存在可见 active）下允许主动改选。
const selectorDisabled = computed(() => workspaceState.value === 'loading' || workspaceState.value === 'error')

// ===== 视图意图（route 单一事实来源） =====
interface ViewIntent {
  pageId: string | null
  wsId: string | null
  q: string
  category: string | null
}

function readIntentFromRoute(): ViewIntent {
  return {
    pageId: queryString(route.params.pageId) || null,
    wsId: queryString(route.query.workspace_id) || null,
    q: queryString(route.query.q),
    category: queryString(route.query.category) || null,
  }
}

const pages = ref<WikiPageSummary[]>([])
const statusFilter = ref<string | undefined>(undefined)
const searchText = ref('')
const categoryFilter = ref<string | undefined>(undefined)
const currentPageId = ref('')
const detail = ref<WikiDetail | null>(null)
const detailError = ref('')
// 详情末态错误（404/不属于当前工作区等）：同目标下避免反复重试；换页时复位。
const detailBad = ref(false)
const revisions = ref<WikiRevision[]>([])
const showRevisions = ref(false)
const viewingPreview = ref(false)
const diff = ref<WikiDiff | null>(null)

// 编辑
const editingId = ref('')
const editingContent = ref('')
const editingOriginal = ref('')
const editingDirty = computed(() => editingId.value !== '' && editingContent.value !== editingOriginal.value)
let editSeq = 0

// 引用追溯（J-2：仅 Evidence，不再有 Card 追溯）
const evidenceVisible = ref(false)
const evidencePageId = ref('')

// ===== 统一浏览上下文（navigation generation） =====
const navGen = ref(0)
const listLoading = ref(false)
const listError = ref('')
const applied = ref<ViewIntent | null>(null)
const forceListNextApply = ref(false)
let alive = true
let wsLoadPromise: Promise<void> | null = null
let confirmingDiscard = false

// 重建进度
const rebuildMsg = ref('')
let rebuildTimer: ReturnType<typeof setInterval> | null = null
let searchDebounce: ReturnType<typeof setTimeout> | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

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

const emptyCatalogTip = computed(() => {
  if (searchText.value || categoryFilter.value || statusFilter.value) return '无匹配主题'
  return '该工作区暂无可浏览主题'
})

// ---- 错误分类：loading / 空 / 403 / 404 / 服务端 / 网络 需有不同文案 ----
function httpErrorKind(e: any): 'forbidden' | 'notfound' | 'server' | 'http' | 'network' {
  const s = e?.response?.status
  if (s === 403) return 'forbidden'
  if (s === 404) return 'notfound'
  if (s && s >= 500) return 'server'
  if (e?.response) return 'http'
  return 'network'
}

function detailErrorText(e: any): string {
  const kind = httpErrorKind(e)
  if (kind === 'notfound') return '该主题不存在或无权访问'
  if (kind === 'forbidden') return '无权访问该主题'
  if (kind === 'server') return '服务异常，请稍后重试'
  return e?.response?.data?.detail || '网络异常，主题加载失败'
}

function listErrorText(e: any): string {
  const kind = httpErrorKind(e)
  if (kind === 'notfound') return 'Wiki 功能未启用'
  if (kind === 'forbidden') return '无权访问该工作区的主题'
  if (kind === 'server') return '服务异常，请稍后重试'
  return e?.response?.data?.detail || '网络异常，主题加载失败'
}

function queryString(v: unknown): string {
  if (typeof v === 'string') return v
  if (Array.isArray(v) && v.length) return String(v[0])
  return ''
}

// ---- 路由工具：push/replace 与 URL 规范化 ----
type QueryExtra = Record<string, LocationQueryValueRaw | undefined>

function makeLocation(pageId: string | null, extra: QueryExtra): { path: string; query: LocationQueryRaw } {
  const query: LocationQueryRaw = { ...route.query, ...extra }
  return { path: pageId ? `/knowledge/wiki/${pageId}` : '/knowledge/wiki', query }
}

function sameAsCurrent(loc: { path: string; query: LocationQueryRaw }): boolean {
  return router.resolve(loc).fullPath === route.fullPath
}

async function navigate(pageId: string | null, extra: QueryExtra, mode: 'push' | 'replace' = 'push'): Promise<boolean> {
  const loc = makeLocation(pageId, extra)
  if (sameAsCurrent(loc)) return false
  try {
    if (mode === 'replace') await router.replace(loc)
    else await router.push(loc)
    return true
  } catch (e) {
    // 导航被守卫取消/重复 → 忽略（保持当前视图与 URL）
    if (isNavigationFailure(e)) return false
    throw e
  }
}

function normalizeWorkspaceIdInUrl(pageId: string | null, wsId: string): void {
  void navigate(pageId, { workspace_id: wsId }, 'replace')
}

// ---- 编辑会话 ----
function startEdit(sec: WikiSection) {
  editingId.value = sec.id
  editingContent.value = sec.content
  editingOriginal.value = sec.content
  editSeq++
}

function cancelEdit() {
  editingId.value = ''
  editingContent.value = ''
  editingOriginal.value = ''
}

// ---- 瞬态清理 ----
function clearDrawerState() {
  revisions.value = []
  diff.value = null
  showRevisions.value = false
  viewingPreview.value = false
  evidenceVisible.value = false
  detailBad.value = false
}

function closeDetail() {
  currentPageId.value = ''
  detail.value = null
  detailError.value = ''
  clearDrawerState()
  cancelEdit()
}

function clearBrowsingState() {
  pages.value = []
  listError.value = ''
  listLoading.value = false
  closeDetail()
  applied.value = null
}

function setNonReady(state: 'none' | 'inaccessible') {
  workspaceState.value = state
  workspaceId.value = ''
  clearBrowsingState()
}

function readLive(gen: number): boolean {
  return alive && gen === navGen.value && workspaceState.value === 'ready'
}

// ---- 未保存编辑导航守卫（取消后视图/编辑/URL/历史保持一致） ----
function confirmDiscardEdits(): Promise<boolean> {
  return ElMessageBox.confirm(
    '当前主题有未保存的编辑内容，继续将丢弃这些修改。是否继续？',
    '未保存编辑',
    { type: 'warning', confirmButtonText: '继续切换', cancelButtonText: '取消' },
  ).then(() => true).catch(() => false)
}

async function guardTopicNavigation(to: RouteLocationNormalized): Promise<boolean> {
  if (!editingDirty.value) return true
  const fromWs = queryString(route.query.workspace_id)
  const fromPage = queryString(route.params.pageId) || null
  const toWs = queryString(to.query.workspace_id)
  const toPage = queryString(to.params.pageId) || null
  if (fromWs === toWs && fromPage === toPage) return true
  if (confirmingDiscard) return false
  confirmingDiscard = true
  try {
    return await confirmDiscardEdits()
  } finally {
    confirmingDiscard = false
  }
}
onBeforeRouteUpdate(guardTopicNavigation)
onBeforeRouteLeave(guardTopicNavigation)

// ---- 工作区列表 ----
async function fetchWorkspaces(force = false): Promise<void> {
  if (!force && (workspaces.value.length > 0 || wsLoadPromise)) {
    if (wsLoadPromise) await wsLoadPromise
    return
  }
  workspaceState.value = 'loading'
  workspaceLoadError.value = ''
  const p = (async () => {
    try {
      workspaces.value = await wikiWorkspacesApi.list()
    } catch (e: any) {
      workspaces.value = []
      workspaceState.value = 'error'
      workspaceLoadError.value = e?.response?.data?.detail || '网络异常，工作区加载失败'
      clearBrowsingState()
    }
  })()
  wsLoadPromise = p
  try {
    await p
  } finally {
    wsLoadPromise = null
  }
}

async function retryWorkspaces() {
  await fetchWorkspaces(true)
  if (workspaceState.value === 'error' || !alive) return
  await applyRoute()
}

// ---- 主题列表 ----
async function load() {
  if (!workspaceReady.value) return
  const gen = navGen.value
  const ws = workspaceId.value
  listLoading.value = true
  listError.value = ''
  try {
    const data = await wikiApi.list({
      workspaceId: ws,
      status: isAdmin.value ? statusFilter.value : 'published',
      q: searchText.value || undefined,
      category: categoryFilter.value,
    })
    if (!readLive(gen) || workspaceId.value !== ws) return
    pages.value = data
  } catch (e: any) {
    if (!readLive(gen) || workspaceId.value !== ws) return
    pages.value = []
    listError.value = listErrorText(e)
  } finally {
    if (readLive(gen) && workspaceId.value === ws) listLoading.value = false
  }
}

// ---- 主题详情 ----
async function openDetail(pageId: string, opts: { preview?: boolean } = {}) {
  const ws = workspaceId.value
  if (!ws || !alive) return
  const gen = navGen.value
  if (currentPageId.value && currentPageId.value !== pageId) cancelEdit()
  currentPageId.value = pageId
  viewingPreview.value = !!opts.preview
  detail.value = null
  detailError.value = ''
  clearDrawerState()
  try {
    const data = await wikiApi.get(pageId, { preview: opts.preview })
    if (!readLive(gen) || workspaceId.value !== ws || currentPageId.value !== pageId) return
    if (data.workspace_id && data.workspace_id !== workspaceId.value) {
      detailBad.value = true
      detailError.value = '该主题不属于当前工作区'
      return
    }
    detail.value = data
  } catch (e: any) {
    if (!readLive(gen) || workspaceId.value !== ws || currentPageId.value !== pageId) return
    detailBad.value = true
    detailError.value = detailErrorText(e)
  }
}

// 操作完成后的原地刷新（不产生新历史记录）；仅当仍停留在目标上下文时生效。
async function refreshDetail(pageId: string, opts: { preview?: boolean } = {}) {
  const ws = workspaceId.value
  const gen = navGen.value
  if (!ws || !alive) return
  viewingPreview.value = opts.preview ?? viewingPreview.value
  try {
    const data = await wikiApi.get(pageId, { preview: opts.preview })
    if (!readLive(gen) || workspaceId.value !== ws || currentPageId.value !== pageId) return
    if (data.workspace_id && data.workspace_id !== workspaceId.value) {
      detailBad.value = true
      detail.value = null
      detailError.value = '该主题不属于当前工作区'
      return
    }
    detailBad.value = false
    detail.value = data
    detailError.value = ''
  } catch (e: any) {
    if (!readLive(gen) || workspaceId.value !== ws || currentPageId.value !== pageId) return
    detailBad.value = true
    detailError.value = detailErrorText(e)
  }
}

// ---- 统一恢复流程：route → 校验 URL → 选工作区 → 目录 → 详情 ----
async function applyRoute() {
  if (!alive) return
  const gen = ++navGen.value
  if (searchDebounce) { clearTimeout(searchDebounce); searchDebounce = null }

  // 1) 首次/失败后先拿工作区列表
  if (workspaceState.value !== 'error' && workspaces.value.length === 0) {
    await fetchWorkspaces()
    if (!alive || gen !== navGen.value) return
  }
  if (workspaceState.value === 'error') {
    applied.value = null
    return
  }

  const intent = readIntentFromRoute()
  const active = browsableWorkspaces.value

  // 2) 决议工作区
  let wsId = ''
  if (intent.wsId) {
    const hit = active.find((w) => w.id === intent.wsId)
    if (hit) {
      wsId = hit.id
    } else {
      // 指定 workspace 不存在/无权访问/archived → 统一不可访问提示，不静默跳转
      setNonReady('inaccessible')
      return
    }
  } else if (active.length) {
    wsId = active[0].id
  } else {
    setNonReady('none')
    return
  }

  // 3) 无 workspace_id：补默认值并规范化 URL（replace，不产生新历史）
  if (!intent.wsId) {
    normalizeWorkspaceIdInUrl(intent.pageId, wsId)
    // URL 变化后由 route watcher 再次触发完整 apply，这里直接返回避免双加载
    return
  }

  // 4) 就绪工作区
  const wsChanged = workspaceState.value !== 'ready' || workspaceId.value !== wsId
  if (wsChanged) {
    workspaceState.value = 'ready'
    workspaceId.value = wsId
    clearBrowsingState()
  }

  // 5) 过滤条件与 URL 一致
  searchText.value = intent.q
  categoryFilter.value = intent.category || undefined

  const listChange =
    forceListNextApply.value ||
    !applied.value ||
    applied.value.wsId !== wsId ||
    applied.value.q !== intent.q ||
    (applied.value.category || '') !== (intent.category || '')
  forceListNextApply.value = false

  if (listChange) {
    await load()
    if (!alive || gen !== navGen.value) return
  }

  // 6) 详情
  if (intent.pageId) {
    const pageChanged = currentPageId.value !== intent.pageId
    const missing = !pageChanged && detail.value === null && !detailBad.value
    if (pageChanged || missing) {
      await openDetail(intent.pageId)
      if (!alive || gen !== navGen.value) return
    }
  } else if (currentPageId.value) {
    closeDetail()
  }

  applied.value = {
    pageId: currentPageId.value || null,
    wsId,
    q: searchText.value,
    category: categoryFilter.value || null,
  }
}

// ---- 用户动作（全部走 router，历史/URL 由 route 统一恢复） ----
async function openPage(pageId: string) {
  if (workspaceState.value !== 'ready' || !workspaceId.value) return
  // 已是当前主题：详情缺失（错误）时允许原地重试；否则忽略
  if (currentPageId.value === pageId && queryString(route.params.pageId) === pageId) {
    if (!detail.value && detailBad.value) {
      detailBad.value = false
      await refreshDetail(pageId, { preview: viewingPreview.value })
    }
    return
  }
  await navigate(pageId, { workspace_id: workspaceId.value })
}

function onWorkspaceSelect(nextId: string) {
  if (!nextId) return
  if (workspaceState.value === 'ready' && nextId === workspaceId.value) return
  void navigate(null, { workspace_id: nextId })
}

function syncSearchToRoute() {
  const q = searchText.value
  if (queryString(route.query.q) === q) return
  void navigate(null, { q: q || undefined }, 'replace')
}

function syncCategoryToRoute() {
  const cat = categoryFilter.value || undefined
  if ((queryString(route.query.category) || undefined) === cat) return
  void navigate(null, { category: cat }, 'replace')
}

watch(searchText, () => {
  if (searchDebounce) clearTimeout(searchDebounce)
  searchDebounce = setTimeout(syncSearchToRoute, 250)
})

watch(categoryFilter, () => {
  syncCategoryToRoute()
})

watch(() => route.fullPath, () => { applyRoute() }, { immediate: true })

// ---- Revision / Diff ----
async function loadRevisions() {
  const pageId = currentPageId.value
  if (!pageId || !alive) return
  const gen = navGen.value
  const ws = workspaceId.value
  try {
    const data = await wikiApi.revisions(pageId)
    if (!alive || gen !== navGen.value || workspaceId.value !== ws || currentPageId.value !== pageId) return
    revisions.value = data
  } catch (e: any) {
    if (!alive || gen !== navGen.value || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '加载版本历史失败')
  }
}

async function openRevisions() {
  if (!currentPageId.value) return
  showRevisions.value = true
  diff.value = null
  await loadRevisions()
}

async function viewDiff(revisionId: string) {
  const pageId = currentPageId.value
  if (!pageId || !alive) return
  const gen = navGen.value
  const ws = workspaceId.value
  try {
    const data = await wikiApi.diff(pageId, revisionId)
    if (!alive || gen !== navGen.value || workspaceId.value !== ws || currentPageId.value !== pageId) return
    diff.value = data
  } catch (e: any) {
    if (!alive || gen !== navGen.value || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '加载 Diff 失败')
  }
}

function openCitation(c: WikiCitation) {
  if (c.evidence) {
    evidencePageId.value = c.evidence.page_id || ''
    evidenceVisible.value = true
  }
}

// ---- 编辑 / 锁定 / 解锁 / 发布 / 回滚 / 归档（await 前捕获目标 ID） ----
async function saveSection(sec: WikiSection) {
  const pageId = currentPageId.value
  const detailObj = detail.value
  if (!pageId || !detailObj) return
  const revisionId = detailObj.viewing_revision_id || detailObj.preview_revision_id
  if (!revisionId) return
  const ws = workspaceId.value
  const seq = editSeq
  const sectionId = sec.id
  const content = editingContent.value
  try {
    await wikiApi.updateSection(pageId, revisionId, sectionId, content)
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.success('已保存')
    if (editSeq !== seq) return
    cancelEdit()
    viewingPreview.value = true
    await refreshDetail(pageId, { preview: true })
  } catch (e: any) {
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  }
}

async function lockSection(sec: WikiSection) {
  const pageId = currentPageId.value
  const detailObj = detail.value
  if (!pageId || !detailObj) return
  const revisionId = detailObj.viewing_revision_id || detailObj.preview_revision_id
  if (!revisionId) return
  const ws = workspaceId.value
  try {
    await wikiApi.lockSection(pageId, revisionId, sec.id)
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    await refreshDetail(pageId, { preview: viewingPreview.value })
  } catch (e: any) {
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '锁定失败')
  }
}

async function unlockSection(sec: WikiSection) {
  const pageId = currentPageId.value
  const detailObj = detail.value
  if (!pageId || !detailObj) return
  const revisionId = detailObj.viewing_revision_id || detailObj.preview_revision_id
  if (!revisionId) return
  const ws = workspaceId.value
  try {
    await wikiApi.unlockSection(pageId, revisionId, sec.id)
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    await refreshDetail(pageId, { preview: viewingPreview.value })
  } catch (e: any) {
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '解锁失败')
  }
}

async function publishRev(revisionId: string) {
  const pageId = currentPageId.value
  if (!pageId || !detail.value) return
  const ws = workspaceId.value
  try {
    await wikiApi.publish(pageId, revisionId)
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.success('已发布')
    viewingPreview.value = false
    await loadRevisions()
    await refreshDetail(pageId)
  } catch (e: any) {
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '发布失败')
  }
}

async function rollbackRev(revisionId: string) {
  const pageId = currentPageId.value
  if (!pageId || !detail.value) return
  const ws = workspaceId.value
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
    await wikiApi.rollback(pageId, revisionId)
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.success('已回滚')
    await loadRevisions()
    await refreshDetail(pageId)
  } catch (e: any) {
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '回滚失败')
  }
}

async function archivePage() {
  const pageId = currentPageId.value
  if (!pageId || !detail.value) return
  const ws = workspaceId.value
  try {
    await wikiApi.archive(pageId)
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.success('已归档')
    cancelEdit()
    forceListNextApply.value = true
    await navigate(null, {}, 'replace')
  } catch (e: any) {
    if (!alive || workspaceId.value !== ws || currentPageId.value !== pageId) return
    ElMessage.error(e?.response?.data?.detail || '归档失败')
  }
}

// ---- 全局重建（不影响当前工作区，仅管理员） ----
async function rebuild() {
  try {
    await ElMessageBox.confirm(
      '重建主题会为全部工作区（含所有已绑定/自动路由的 Notebook 对应工作区）重新入队后台编译任务，与当前所选工作区无关。是否继续？',
      '全局重建确认',
      { type: 'warning', confirmButtonText: '全部重建', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await wikiApi.rebuild()
    ElMessage.success('已启动全局重建')
    startPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重建失败')
  }
}

async function refreshDirty() {
  try {
    await ElMessageBox.confirm(
      '增量刷新作用于全部工作区的 dirty Wiki / dirty Page 后台任务，与当前所选工作区无关。是否继续？',
      '全局增量刷新确认',
      { type: 'warning', confirmButtonText: '全部刷新', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await wikiApi.refreshDirty()
    ElMessage.success('已提交后台刷新')
    startRefreshPolling()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '增量刷新失败')
  }
}

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

onBeforeUnmount(() => {
  alive = false
  navGen.value++
  stopPolling()
  stopRefreshPolling()
  if (searchDebounce) clearTimeout(searchDebounce)
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
.workspace-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.ws-label {
  font-size: 13px;
  color: #6b7280;
  font-weight: 600;
}
.ws-current-name {
  font-size: 13px;
  color: #111827;
  font-weight: 600;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wiki-stats { font-size: 13px; color: #6b7280; }
.wiki-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.global-manage {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  border: 1px dashed #d1d5db;
  border-radius: 8px;
  padding: 6px 12px;
  background: #fafafa;
}
.gm-title { font-size: 12px; font-weight: 700; color: #6b7280; }
.gm-note { font-size: 12px; color: #9ca3af; }
.gm-actions { display: flex; gap: 8px; align-items: center; }
.rebuild-banner { font-size: 13px; color: #b45309; background: #fffbeb; border: 1px solid #f59e0b; border-radius: 6px; padding: 6px 12px; }
.wiki-status {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  font-size: 14px;
  color: #6b7280;
  min-height: 160px;
}
.wiki-status.status-error { color: #b91c1c; }
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
  min-width: 0;
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
  gap: 8px;
}
.catalog-item:hover { background: #f8fafc; }
.catalog-item.active { background: #eff6ff; border-color: #3b82f6; }
.catalog-title { font-size: 13px; color: #111827; font-weight: 500; overflow: hidden; text-overflow: ellipsis; }
.catalog-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: #b91c1c;
  font-size: 13px;
  padding: 24px;
  text-align: center;
}
.detail-error {
  font-size: 13px;
  color: #b91c1c;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 16px;
  text-align: center;
  margin: auto;
  width: 100%;
}
.detail-content { overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }
.breadcrumb { display: flex; gap: 12px; font-size: 12px; color: #9ca3af; }
.detail-header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.detail-title { font-size: 18px; font-weight: 700; color: #111827; margin: 0; }
.detail-summary { font-size: 14px; color: #4b5563; background: #f8fafc; border-radius: 6px; padding: 10px 12px; margin: 0; }
.trace-box { font-size: 12px; color: #6b7280; background: #f9fafb; border-radius: 6px; padding: 8px 10px; }
.trace-line { margin-top: 4px; word-break: break-all; }
.section-item { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; }
.section-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
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
.rev-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border: 1px solid #f3f4f6; border-radius: 6px; flex-wrap: wrap; }
.rev-meta { font-size: 12px; color: #6b7280; flex: 1; }
.diff-box { margin-top: 10px; border: 1px dashed #f59e0b; border-radius: 8px; padding: 10px; background: #fffbeb; display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.diff-add { color: #10b981; }
.diff-remove { color: #ef4444; }
.diff-change { color: #f59e0b; }
.empty-tip { font-size: 13px; color: #9ca3af; padding: 24px; text-align: center; }

@media (max-width: 720px) {
  .wiki-page {
    padding: 12px;
    height: auto;
    overflow-y: auto;
    overflow-x: hidden;
  }
  .wiki-body {
    display: flex;
    flex-direction: column;
    overflow: visible;
  }
  .catalog-pane {
    max-height: 40vh;
    flex: none;
  }
  .detail-pane {
    min-height: 55vh;
  }
}
</style>
