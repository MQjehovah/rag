<template>
  <section class="admin-runs-panel" :class="{ 'is-collapsed': collapsed }">
    <div class="arp-head">
      <span class="arp-title">当前工作区编译任务</span>
      <el-tag
        v-if="!collapsed && !loadError && !pollError && pageActiveCount > 0"
        size="small"
        type="warning"
      >当前页进行中 {{ pageActiveCount }}</el-tag>
      <el-tag v-if="!collapsed && !loadError && total > 0" size="small" type="info" effect="plain">全工作区匹配 {{ total }}</el-tag>
      <div class="arp-actions">
        <el-button v-if="pollError" size="small" class="arp-retry-refresh" @click="loadPage('manual')">重试刷新</el-button>
        <el-button
          v-if="!collapsed"
          size="small"
          class="arp-refresh"
          :disabled="fetching || loading"
          @click="loadPage('manual')"
        >刷新</el-button>
        <el-button size="small" class="arp-toggle" @click="toggleCollapse">{{ collapsed ? '展开' : '收起' }}</el-button>
      </div>
    </div>

    <div v-if="!collapsed" v-loading="loading && runs.length === 0" class="arp-body">
      <div v-if="loadError" class="arp-state arp-error">
        <span>{{ loadError }}</span>
        <el-button size="small" type="primary" class="arp-load-retry" @click="loadPage('manual')">重试</el-button>
      </div>
      <div v-else-if="pollError" class="arp-state arp-poll-warn">
        <span>自动刷新已停止：{{ pollError }}</span>
        <el-button size="small" type="primary" class="arp-poll-retry" @click="loadPage('manual')">重试刷新</el-button>
      </div>
      <div v-else-if="runs.length === 0" class="arp-state">当前工作区暂无编译任务</div>

      <template v-else>
        <div class="arp-list-scroll">
          <div class="arp-list">
            <div
              v-for="run in runs"
              :key="run.id"
              class="compile-run-row"
              :data-run-id="run.id"
            >
            <div class="run-head" @click="toggleExpand(run)">
              <code class="run-pipeline">{{ run.pipeline_key || '未知流水线' }}</code>
              <el-tag v-if="run.pipeline_version" size="small" type="info" effect="plain">
                v{{ run.pipeline_version }}
              </el-tag>
              <el-tag size="small" :type="statusTag(run.status)">{{ statusLabel(run.status) }}</el-tag>
              <el-tag v-if="run.status !== 'cancelled' && run.cancel_requested" size="small" type="warning" effect="dark">
                取消请求中
              </el-tag>
              <span class="run-trigger">{{ triggerLabel(run.trigger_type) }}</span>
              <span class="run-attempt">attempt {{ run.attempt ?? 0 }}</span>
              <span class="run-expand-arrow">{{ expandedId === run.id ? '收起' : '展开' }}</span>
            </div>

            <div class="run-times">
              <span v-if="run.created_at">创建 {{ fmtTs(run.created_at) }}</span>
              <span v-if="run.started_at">开始 {{ fmtTs(run.started_at) }}</span>
              <span v-if="run.finished_at">完成 {{ fmtTs(run.finished_at) }}</span>
            </div>

            <!-- safe error：仅展示净化后的 safe_error / error_summary -->
            <div v-if="run.safe_error_message || run.error_summary" class="run-safe-error">
              {{ run.safe_error_message || run.error_summary }}
              <span v-if="run.safe_error_code" class="run-safe-code">（{{ run.safe_error_code }}）</span>
            </div>

            <div class="run-actions">
              <el-button
                v-if="run.status === 'failed'"
                size="small"
                type="primary"
                class="run-retry-btn"
                :loading="busyId === run.id"
                :disabled="busyId !== null"
                @click.stop="retryRun(run)"
              >重试</el-button>
              <el-button
                v-if="(run.status === 'queued' || run.status === 'running') && !run.cancel_requested"
                size="small"
                type="warning"
                class="run-cancel-btn"
                :loading="busyId === run.id"
                :disabled="busyId !== null"
                @click.stop="cancelRun(run)"
              >取消</el-button>
            </div>

            <!-- 展开：getRun stage 时间线（不含 artifact payload）；每次展开/每轮刷新都会重新请求 -->
            <div v-if="expandedId === run.id" class="run-stages">
              <div v-if="stageLoading === run.id" v-loading="true" class="arp-state">加载阶段信息…</div>
              <div v-else-if="stageErrorMap[run.id]" class="arp-state arp-error">
                <span>{{ stageErrorMap[run.id] }}</span>
              </div>
              <div v-else-if="!stagesMap[run.id] || stagesMap[run.id].length === 0" class="arp-state">
                该任务暂无可展示的阶段信息
              </div>
              <div v-else class="run-stage-list">
                <div
                  v-for="st in stagesMap[run.id]"
                  :key="st.id"
                  class="run-stage-row"
                >
                  <span class="run-stage-order">{{ st.stage_order ?? '-' }}</span>
                  <span class="run-stage-key">{{ st.stage_key || st.component_key || '未知阶段' }}</span>
                  <el-tag size="small" :type="stageTag(st.status)">{{ stageLabel(st.status) }}</el-tag>
                  <span v-if="st.attempt != null" class="run-stage-attempt">attempt {{ st.attempt }}</span>
                  <span v-if="st.started_at" class="run-stage-time">开始 {{ fmtTs(st.started_at) }}</span>
                  <span v-if="st.finished_at" class="run-stage-time">完成 {{ fmtTs(st.finished_at) }}</span>
                  <div v-if="st.safe_error_message" class="run-stage-error">{{ st.safe_error_message }}</div>
                </div>
              </div>
            </div>
            </div>
          </div>
        </div>

        <!-- 分页：total 为服务端全量匹配数；当前页条数与之分开 -->
        <div class="arp-footer">
          <div class="arp-footer-info">
            <span class="arp-page-label">第 {{ page }} / {{ totalPages }} 页</span>
            <span class="arp-page-count">当前页 {{ runs.length }} 条</span>
            <span class="arp-total-count">全工作区匹配 {{ total }} 条</span>
          </div>
          <div class="arp-footer-actions">
            <el-button
              size="small"
              class="arp-prev"
              :disabled="!canPrev || fetching"
              @click="goPage(-1)"
            >上一页</el-button>
            <el-button
              size="small"
              class="arp-next"
              :disabled="!canNext || fetching"
              @click="goPage(1)"
            >下一页</el-button>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  wikiCompileApi,
  type CompileRunDetail,
  type CompileRunSummary,
  type CompileStageRun,
} from '../../api/wikiCompile'

const props = withDefaults(defineProps<{
  workspaceId: string
  /** 面板是否可见（父控制；默认可见）。关闭（收起/不可见）即停止轮询。 */
  visible?: boolean
}>(), {
  visible: true,
})

const PAGE_SIZE = 20
const POLL_INTERVAL_MS = 1000

const STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  superseded: '已被替代',
}

function statusLabel(s: string | null | undefined): string {
  return STATUS_LABELS[s || ''] || String(s || '未知')
}

function statusTag(s: string | null | undefined): 'success' | 'danger' | 'warning' | 'info' {
  if (s === 'succeeded') return 'success'
  if (s === 'failed' || s === 'cancelled') return 'danger'
  if (s === 'running' || s === 'queued') return 'warning'
  return 'info'
}

const TRIGGER_LABELS: Record<string, string> = {
  manual: '手动触发',
  auto: '自动触发',
  refresh: '增量刷新',
  batch: '批量触发',
}

function triggerLabel(t: string | null | undefined): string {
  return TRIGGER_LABELS[t || ''] || String(t || '未知触发')
}

function stageLabel(s: string | null | undefined): string {
  return STATUS_LABELS[s || ''] || String(s || '未知')
}

function stageTag(s: string | null | undefined): 'success' | 'danger' | 'warning' | 'info' {
  return statusTag(s)
}

// ---- 状态（ctxGen 语义：切 workspace / 收起再展开即更换上下文，旧在途请求一律失效） ----
const runs = ref<CompileRunSummary[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const fetching = ref(false)
const loadError = ref('')
const pollError = ref('')
const collapsed = ref(false)
const expandedId = ref('')
const stageLoading = ref('')
const stageErrorMap = ref<Record<string, string>>({})
const stagesMap = ref<Record<string, CompileStageRun[]>>({})
const busyId = ref<string | null>(null)
const ctxGen = ref(0)
// 同一 run 的 getRun 请求序号：收起→重开时第二次请求可覆盖第一次在途结果，旧响应不得改写新 Stage。
const stageSeqMap = ref<Record<string, number>>({})
// 同一 run 的在途 getRun 请求序号（0=无在途）：轮询驱动刷新在途时只登记待刷新，不叠加新请求，
// 避免“单次 getRun 持续 > 轮询周期”时请求无界叠加与展开区持续 loading。
const stageInflightSeq = ref<Record<string, number>>({})
// 在途详情返回后仍展开且期间有“待反映真实状态”的刷新请求 → 恰好补一次（不重叠）。
const stageRefreshQueued = ref<Record<string, boolean>>({})
// action（retry/cancel）完成时 listRuns 已在途 → 标记待刷新，当前请求结束后补一次（不重叠、ctx 失效即清除）。
const pendingRefresh = ref(false)

let pollTimer: ReturnType<typeof setTimeout> | null = null
let alive = true

const pageActiveCount = computed(
  () => runs.value.filter((r) => r.status === 'queued' || r.status === 'running').length,
)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const canPrev = computed(() => page.value > 1)
const canNext = computed(() => page.value < totalPages.value)

function panelOpen(): boolean {
  return alive && props.visible !== false && !collapsed.value && Boolean(props.workspaceId)
}

function shouldPoll(): boolean {
  return (
    panelOpen() &&
    pollError.value === '' &&
    loadError.value === '' &&
    runs.value.length > 0 &&
    pageActiveCount.value > 0
  )
}

function scheduleNext() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
  if (!shouldPoll()) return
  pollTimer = setTimeout(() => {
    pollTimer = null
    void loadPage('poll')
  }, POLL_INTERVAL_MS)
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function fmtTs(t: string | null): string {
  return (t || '').replace('T', ' ').slice(5, 16)
}

/** 切工作区 / 关闭再打开：重置上下文与分页并加载；旧请求 finally 不得覆盖新 loading/fetching。 */
function resetContext() {
  stopPolling()
  ctxGen.value++
  runs.value = []
  total.value = 0
  page.value = 1
  loading.value = false
  fetching.value = false
  pendingRefresh.value = false
  expandedId.value = ''
  stageLoading.value = ''
  stagesMap.value = {}
  stageErrorMap.value = {}
  stageSeqMap.value = {}
  stageInflightSeq.value = {}
  stageRefreshQueued.value = {}
  loadError.value = ''
  pollError.value = ''
}

async function loadPage(origin: 'manual' | 'poll' | 'action' | 'open' | 'page') {
  const ws = props.workspaceId
  if (!ws || !panelOpen()) return
  if (fetching.value) {
    // 同一 ctx 内请求不重叠（含手动刷新与轮询）；action 请求需补一次最终刷新
    if (origin === 'action') pendingRefresh.value = true
    return
  }
  const ctx = ctxGen.value
  fetching.value = true
  if (runs.value.length === 0) loading.value = true
  try {
    const data = await wikiCompileApi.listRuns({
      workspaceId: ws,
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
    })
    if (!alive || ctx !== ctxGen.value || ws !== props.workspaceId) return
    runs.value = data.runs || []
    total.value = data.total ?? runs.value.length
    loadError.value = ''
    pollError.value = ''
    // 已消失的 run 清掉展开/缓存，避免陈旧阶段
    const liveIds = new Set(runs.value.map((r) => r.id))
    for (const rid of Object.keys(stagesMap.value)) {
      if (!liveIds.has(rid)) {
        delete stagesMap.value[rid]
        delete stageSeqMap.value[rid]
        delete stageInflightSeq.value[rid]
        delete stageRefreshQueued.value[rid]
      }
    }
    if (expandedId.value && !liveIds.has(expandedId.value)) {
      const gone = expandedId.value
      expandedId.value = ''
      stageLoading.value = ''
      delete stageSeqMap.value[gone]
      delete stageInflightSeq.value[gone]
      delete stageRefreshQueued.value[gone]
    }
    // 当前展开 run 随真实状态刷新（列表 status/attempt/current_stage 变化或每轮轮询都触发）
    void refreshExpandedStages()
  } catch (e: any) {
    if (!alive || ctx !== ctxGen.value || ws !== props.workspaceId) return
    if (origin === 'poll') {
      pollError.value = e?.response?.data?.detail || '网络异常，自动刷新已停止'
      stopPolling()
    } else {
      loadError.value = e?.response?.data?.detail || '加载编译任务失败'
    }
  } finally {
    if (alive && ctx === ctxGen.value && ws === props.workspaceId) {
      loading.value = false
      fetching.value = false
      if (pendingRefresh.value) {
        // 操作后的必要刷新被在途请求吞掉 → 补一次（不重叠；此时 fetching 已复位）
        pendingRefresh.value = false
        void loadPage('action')
        return
      }
      scheduleNext()
    }
  }
}

async function refreshExpandedStages() {
  const rid = expandedId.value
  if (!rid || !panelOpen()) return
  const run = runs.value.find((r) => r.id === rid)
  if (!run) return
  await fetchStages(run)
}

async function toggleExpand(run: CompileRunSummary) {
  if (!panelOpen()) return
  if (expandedId.value === run.id) {
    expandedId.value = ''
    stageLoading.value = ''
    stageSeqMap.value[run.id] = (stageSeqMap.value[run.id] || 0) + 1
    stageRefreshQueued.value[run.id] = false
    return
  }
  expandedId.value = run.id
  stageErrorMap.value[run.id] = ''
  // 再次展开一律重新请求（不允许永久命中旧缓存；序号使旧在途 getRun 失效）
  await fetchStages(run)
}

async function fetchStages(run: CompileRunSummary) {
  const ctx = ctxGen.value
  const ws = props.workspaceId
  const rid = run.id
  if (!alive || !panelOpen()) return
  // 在途去重：若该 run 已有 getRun 在途且其序号仍为最新（未被收起/重开打断），
  // 轮询/列表刷新驱动的详情请求不叠加，只登记“待刷新”，由本次在途返回后恰好补一次。
  const curSeq = stageSeqMap.value[rid] || 0
  if (stageInflightSeq.value[rid] !== 0 && stageInflightSeq.value[rid] === curSeq) {
    stageRefreshQueued.value[rid] = true
    return
  }
  // 序号：新请求可覆盖同一 run 的旧在途请求（如收起→重开显式重取）；旧响应/其 finally 不得改写新 Stage/loading。
  const seq = curSeq + 1
  stageSeqMap.value[rid] = seq
  stageInflightSeq.value[rid] = seq
  stageLoading.value = rid
  try {
    const detail = await wikiCompileApi.getRun(rid)
    // getRun 结果校验 workspace/run/ctxGen/请求序号：旧详情不覆盖新工作区或新 Stage。
    if (
      !alive ||
      ctx !== ctxGen.value ||
      ws !== props.workspaceId ||
      expandedId.value !== rid ||
      stageSeqMap.value[rid] !== seq
    ) return
    if (detail.workspace_id !== ws) return
    applyStageDetail(detail)
  } catch (e: any) {
    if (
      !alive || ctx !== ctxGen.value || ws !== props.workspaceId ||
      expandedId.value !== rid || stageSeqMap.value[rid] !== seq
    ) return
    stageErrorMap.value[rid] = e?.response?.data?.detail || '阶段信息加载失败'
  } finally {
    // 仅当自己是当前在途请求时才清除在途标记（避免误清重开后更新的在途请求）。
    if (stageInflightSeq.value[rid] === seq) stageInflightSeq.value[rid] = 0
    if (alive && ctx === ctxGen.value && ws === props.workspaceId &&
        stageSeqMap.value[rid] === seq) {
      stageLoading.value = ''
      if (stageRefreshQueued.value[rid]) {
        // 在途期间的刷新诉求补一次（不重叠：此处在途已清，fetchStages 会重新开始新请求）
        stageRefreshQueued.value[rid] = false
        const runNow = runs.value.find((r) => r.id === rid)
        if (runNow && expandedId.value === rid) void fetchStages(runNow)
      }
    }
  }
}

function applyStageDetail(detail: CompileRunDetail) {
  const run = runs.value.find((r) => r.id === detail.id)
  if (run) {
    // 详情 run 字段（attempt/status/current_stage）与列表同步，展示真实状态
    run.attempt = detail.attempt
    run.status = detail.status
    run.current_stage = detail.current_stage
    run.cancel_requested = detail.cancel_requested
    run.safe_error_code = detail.safe_error_code
    run.safe_error_message = detail.safe_error_message
    run.finished_at = detail.finished_at
  }
  stagesMap.value[detail.id] = detail.stages || []
  stageErrorMap.value[detail.id] = ''
}

async function retryRun(run: CompileRunSummary) {
  if (busyId.value) return
  const ctx = ctxGen.value
  const ws = props.workspaceId
  busyId.value = run.id
  try {
    await wikiCompileApi.retryRun(run.id)
    ElMessage.success('已重新入队')
  } catch (e: any) {
    // retry 409 → 受控提示并刷新真实状态，不新建 run（服务端为最终依据）
    const msg = e?.response?.data?.detail || '重试失败'
    ElMessage.warning(msg)
  } finally {
    busyId.value = null
    if (alive && ctx === ctxGen.value && ws === props.workspaceId) {
      await loadPage('action')
    }
  }
}

async function cancelRun(run: CompileRunSummary) {
  if (busyId.value) return
  const ctx = ctxGen.value
  const ws = props.workspaceId
  busyId.value = run.id
  try {
    await wikiCompileApi.cancelRun(run.id)
    // 提交后刷新真实状态：不假装已取消（queued→cancelled / running→cancel_requested 由服务端返回驱动）
  } catch (e: any) {
    const msg = e?.response?.data?.detail || '取消失败'
    ElMessage.error(msg)
  } finally {
    busyId.value = null
    if (alive && ctx === ctxGen.value && ws === props.workspaceId) {
      await loadPage('action')
    }
  }
}

function goPage(delta: number) {
  const target = page.value + delta
  if (target < 1 || target > totalPages.value) return
  page.value = target
  // 翻页后清展开与缓存；轮询判断按当前页重置（当前页若非终态继续轮询）
  expandedId.value = ''
  stageLoading.value = ''
  stagesMap.value = {}
  stageErrorMap.value = {}
  stageSeqMap.value = {}
  stageInflightSeq.value = {}
  stageRefreshQueued.value = {}
  void loadPage('page')
}

function toggleCollapse() {
  collapsed.value = !collapsed.value
  if (collapsed.value) {
    // 收起：停止轮询并失效在途请求（不永久命中旧 fetching）
    stopPolling()
    ctxGen.value++
    pendingRefresh.value = false
    expandedId.value = ''
    stageLoading.value = ''
    runs.value = []
    stagesMap.value = {}
    stageErrorMap.value = {}
    stageSeqMap.value = {}
    stageInflightSeq.value = {}
    stageRefreshQueued.value = {}
  } else {
    // 展开：旧 fetching 不得阻塞新上下文
    stopPolling()
    ctxGen.value++
    pendingRefresh.value = false
    loading.value = false
    fetching.value = false
    expandedId.value = ''
    stageLoading.value = ''
    stageSeqMap.value = {}
    stageInflightSeq.value = {}
    stageRefreshQueued.value = {}
    loadError.value = ''
    pollError.value = ''
    void loadPage('open')
  }
}

watch(
  () => [props.workspaceId, props.visible],
  ([ws, visible]) => {
    resetContext()
    if (visible !== false && ws) {
      void loadPage('open')
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  alive = false
  stopPolling()
  ctxGen.value++
})
</script>

<style scoped>
.admin-runs-panel {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fafbfc;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.admin-runs-panel:not(.is-collapsed) {
  max-height: clamp(300px, 55vh, 620px);
}
.arp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  flex-wrap: wrap;
  flex: 0 0 auto;
}
.arp-title {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
}
.arp-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.arp-body {
  border-top: 1px solid #f3f4f6;
  padding: 10px 12px;
  min-height: 0;
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
}
.arp-state {
  font-size: 12px;
  color: #9ca3af;
  padding: 10px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.arp-error {
  color: #b91c1c;
}
.arp-poll-warn {
  color: #b45309;
  background: #fffbeb;
  border-radius: 6px;
}
.arp-list-scroll {
  overflow-x: hidden;
  overflow-y: auto;
  min-height: 0;
  flex: 1 1 auto;
  scrollbar-gutter: stable;
}
.arp-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.compile-run-row {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 10px;
  background: #fff;
}
.run-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  cursor: pointer;
}
.run-pipeline {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 600;
  color: #111827;
}
.run-trigger {
  font-size: 12px;
  color: #6b7280;
  background: #f3f4f6;
  border-radius: 6px;
  padding: 1px 8px;
}
.run-attempt {
  font-size: 12px;
  color: #9ca3af;
}
.run-expand-arrow {
  margin-left: auto;
  font-size: 12px;
  color: #2563eb;
}
.run-times {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: #9ca3af;
  margin-top: 6px;
}
.run-safe-error {
  margin-top: 6px;
  font-size: 12px;
  color: #b91c1c;
  background: #fef2f2;
  border-radius: 6px;
  padding: 4px 8px;
  overflow-wrap: anywhere;
}
.run-safe-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.run-actions {
  margin-top: 8px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.run-stages {
  margin-top: 8px;
  border-top: 1px dashed #e5e7eb;
  padding-top: 8px;
}
.run-stage-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.run-stage-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
  color: #4b5563;
  border-left: 3px solid #e5e7eb;
  padding-left: 8px;
}
.run-stage-order {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: #9ca3af;
}
.run-stage-key {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 500;
  color: #374151;
}
.run-stage-attempt,
.run-stage-time {
  color: #9ca3af;
}
.run-stage-error {
  width: 100%;
  color: #b91c1c;
  overflow-wrap: anywhere;
}
.arp-footer {
  margin-top: 10px;
  border-top: 1px dashed #e5e7eb;
  padding-top: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  flex: 0 0 auto;
}
@media (max-width: 720px) {
  .admin-runs-panel:not(.is-collapsed) {
    max-height: clamp(240px, 52vh, 480px);
  }
}
.arp-footer-info {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12px;
  color: #6b7280;
}
.arp-page-count,
.arp-total-count {
  color: #374151;
}
.arp-footer-actions {
  display: flex;
  gap: 6px;
}
</style>
