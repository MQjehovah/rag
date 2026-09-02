<template>
  <el-drawer
    v-model="visible"
    :title="detail?.canonical_title || '卡片详情'"
    size="720px"
    direction="rtl"
    @closed="onClosed"
  >
    <div v-loading="loading" class="card-detail-body">
      <!-- 错误态：404/500 区分 + 重试 -->
      <div v-if="error" class="error-box">
        <p class="error-text">{{ error }}</p>
        <el-button type="primary" size="small" @click="load">重试</el-button>
      </div>

      <div v-else-if="!loading && detail" class="detail-content">
        <!-- 头部：类型/状态/风险/置信度 -->
        <div class="detail-header">
          <el-tag :type="typeColor" effect="dark" size="small">{{ typeLabel }}</el-tag>
          <el-tag :type="statusColor" size="small">{{ statusLabel }}</el-tag>
          <el-tag v-if="detail.risk_level" size="small">风险 {{ riskLabel }}</el-tag>
          <span class="confidence">置信度 {{ (detail.confidence * 100).toFixed(0) }}%</span>
        </div>

        <!-- 版本标识 + 切换按钮 -->
        <div class="version-bar">
          <el-tag :type="currentView === 'review' ? 'warning' : 'success'" size="small">
            当前查看：{{ versionLabel }}
          </el-tag>
          <el-button
            v-if="isAdmin && detail.has_draft_revision"
            size="small"
            text
            type="primary"
            @click="toggleRevisionMode"
          >
            切换到{{ currentView === 'published' ? '待审版本' : '线上可信版本' }}
          </el-button>
        </div>

        <p v-if="detail.summary" class="detail-summary">{{ detail.summary }}</p>

        <!-- 派生图谱状态（只读，展示知识是否已入图） -->
        <div class="graph-status-row">
          <el-tag :type="graphStatusTag" size="small">{{ graphStatusLabel }}</el-tag>
          <span class="graph-status-meta">
            实体 {{ detail.entity_count ?? 0 }} · 关系 {{ detail.relation_count ?? 0 }} · 社区 {{ detail.community_count ?? 0 }}
          </span>
        </div>

        <el-tabs v-model="activeTab">
          <!-- 可信内容 -->
          <el-tab-pane label="可信内容" name="content">
            <div v-if="detail.blocks.length" class="blocks-list">
              <div v-for="block in detail.blocks" :key="block.order_index + block.block_type" class="block-item">
                <div class="block-head">
                  <el-tag size="small" type="info">{{ BLOCK_TYPE_LABELS[block.block_type] || block.block_type }}</el-tag>
                  <span class="block-heading">{{ block.heading }}</span>
                </div>
                <div class="block-content">{{ block.content }}</div>
              </div>
            </div>
            <div v-else-if="detail.revision_body" class="revision-body">
              {{ detail.revision_body }}
            </div>
            <div v-else-if="detail.claims.length" class="placeholder">
              该 Card 以可信声明为主要内容（见「可信声明」页签）。
            </div>
            <div v-else class="placeholder">
              该 Card 缺少可阅读内容，需要重新编译或补充。
            </div>
          </el-tab-pane>

          <!-- 可信声明 -->
          <el-tab-pane label="可信声明" name="claims">
            <div v-if="detail.claims.length" class="claims-list">
              <div v-for="(claim, idx) in detail.claims" :key="idx" class="claim-item">
                <div class="claim-head">
                  <el-tag size="small" :type="CLAIM_TYPE_TAG[claim.claim_type] || 'info'">
                    {{ CLAIM_TYPE_LABELS[claim.claim_type] || claim.claim_type }}
                  </el-tag>
                  <el-tag v-if="claim.status !== 'active'" size="small" type="warning">
                    {{ claim.status }}
                  </el-tag>
                  <span class="claim-meta">置信度 {{ (claim.confidence * 100).toFixed(0) }}%</span>
                  <span class="claim-meta">证据 {{ claim.evidence_count ?? 0 }} 条</span>
                </div>
                <div class="claim-statement">{{ claim.statement }}</div>
              </div>
            </div>
            <div v-else class="placeholder">无可信声明</div>
          </el-tab-pane>

          <!-- 证据与来源 -->
          <el-tab-pane label="证据与来源" name="evidence">
            <h4 class="tab-section-title">来源</h4>
            <div v-if="sources.length" class="source-list">
              <div v-for="s in sources" :key="s.id" class="source-item">
                <div class="source-head">
                  <el-tag size="small" type="success">{{ contributionLabel(s.contribution_type) }}</el-tag>
                  <span class="source-title">{{ s.page_title || '未命名来源' }}</span>
                  <el-tag v-if="s.source_type" size="small" type="info">{{ s.source_type }}</el-tag>
                </div>
                <div class="source-meta">
                  <span v-if="s.page_number != null">页码 {{ s.page_number }}</span>
                  <span v-if="s.heading">章节 {{ s.heading }}</span>
                  <span v-if="s.section_path">定位 {{ s.section_path }}</span>
                  <el-tag v-if="s.evidence_needs_review" size="small" type="warning">需人工审核</el-tag>
                </div>
                <el-button
                  v-if="s.source_url || s.page_id"
                  size="small"
                  text
                  type="primary"
                  @click="openSource(s)"
                >
                  查看原始资料
                </el-button>
              </div>
            </div>
            <div v-else class="placeholder">无来源信息</div>

            <h4 class="tab-section-title">证据</h4>
            <div v-if="evidences.length" class="evidence-list">
              <div v-for="ev in evidences" :key="ev.id" class="evidence-item">
                <div class="evidence-head">
                  <el-tag size="small" type="info">{{ ev.evidence_type }}</el-tag>
                  <el-tag v-if="ev.inferred" size="small" type="warning">含推断</el-tag>
                  <el-tag v-if="ev.needs_review" size="small" type="warning">需人工审核</el-tag>
                </div>
                <div class="evidence-content">{{ ev.content }}</div>
                <div v-for="obs in ev.observations || []" :key="obs.id" class="observation-row">
                  <el-tag size="small">{{ obs.observation_type }}</el-tag>
                  <el-tag v-if="obs.inferred" size="small" type="warning">inferred=true</el-tag>
                  <el-tag v-if="obs.needs_review" size="small" type="warning">需审核</el-tag>
                  <span>{{ obs.content }}</span>
                </div>
              </div>
            </div>
            <div v-else class="placeholder">无关联 Evidence</div>
          </el-tab-pane>

          <!-- 图谱关系 -->
          <el-tab-pane label="图谱关系" name="graph">
            <div v-if="graphError" class="placeholder">图谱功能未启用或暂无关系</div>
            <div v-else class="graph-relation">
              <div class="graph-entities">
                <h4 class="tab-section-title">关联实体（{{ graphDetail?.entities.length ?? 0 }}）</h4>
                <div v-if="graphDetail?.entities.length" class="tag-row">
                  <el-tag v-for="e in graphDetail.entities" :key="e.id" size="small" type="info">
                    {{ e.name }}（{{ ENTITY_TYPE_LABELS[e.entity_type] || e.entity_type }}）
                  </el-tag>
                </div>
                <div v-else class="placeholder">无关联实体</div>
              </div>
              <div class="graph-relations">
                <h4 class="tab-section-title">关系（{{ graphDetail?.relations.length ?? 0 }}）</h4>
                <div v-if="graphDetail?.relations.length" class="relation-list">
                  <div v-for="r in graphDetail.relations" :key="r.id" class="relation-item">
                    <span>{{ r.source_entity_name }}</span>
                    <el-tag size="small" type="primary">{{ RELATION_LABELS[r.relation_type] || r.relation_type }}</el-tag>
                    <span>{{ r.target_entity_name }}</span>
                  </div>
                </div>
                <div v-else class="placeholder">无关系</div>
              </div>
              <div class="graph-communities">
                <h4 class="tab-section-title">所属 Community（{{ graphDetail?.communities.length ?? 0 }}）</h4>
                <div v-if="graphDetail?.communities.length" class="tag-row">
                  <el-tag v-for="c in graphDetail.communities" :key="c.id" size="small" type="success">
                    {{ c.title }}
                  </el-tag>
                </div>
                <div v-else class="placeholder">不属于任何 Community</div>
              </div>
            </div>
          </el-tab-pane>

          <!-- 版本历史 -->
          <el-tab-pane label="版本历史" name="revisions">
            <div class="revisions-list">
              <div v-for="rev in revisions" :key="rev.id" class="revision-item" @click="selectRevision(rev)">
                <el-tag size="small" :type="changeColor(rev.change_type)">{{ rev.change_type }}</el-tag>
                <span class="revision-meta">{{ rev.created_at?.slice(0, 10) }} {{ rev.status }}</span>
              </div>
              <div v-if="!revisions.length" class="placeholder">无 Revision</div>

              <div v-if="diffResult && diffTarget" class="diff-box">
                <h4>Diff（当前 vs {{ diffTarget.change_type }}）</h4>
                <div class="diff-summary">
                  +{{ diffResult.diff.added_blocks }} blocks / -{{ diffResult.diff.removed_blocks }} blocks /
                  ~{{ diffResult.diff.changed_blocks }} changed / +{{ diffResult.diff.added_claims }} claims
                </div>
                <div v-if="diffResult.diff.added_blocks || diffResult.diff.removed_blocks || diffResult.diff.changed_blocks" class="diff-detail">
                  <div v-for="b in diffResult.changes.added_blocks" :key="'a' + b.heading" class="diff-add">
                    + {{ b.heading }}: {{ b.new_content }}
                  </div>
                  <div v-for="b in diffResult.changes.removed_blocks" :key="'r' + b.heading" class="diff-remove">
                    - {{ b.heading }}: {{ b.old_content }}
                  </div>
                  <div v-for="b in diffResult.changes.changed_blocks" :key="'c' + b.heading" class="diff-change">
                    ~ {{ b.heading }}: {{ b.old_content }} → {{ b.new_content }}
                  </div>
                </div>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>

        <!-- 审核操作（仅在 review 模式显示） -->
        <footer v-if="isAdmin && currentView === 'review'" class="detail-actions">
          <el-button
            v-if="canReview"
            type="success"
            :loading="reviewing"
            @click="approve"
          >
            通过并发布
          </el-button>
          <el-button v-if="canReject" type="danger" @click="reject">整卡驳回</el-button>
          <el-button v-if="detail?.status !== 'archived'" @click="archive">归档</el-button>
        </footer>
      </div>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  cardApi,
  type CardDetail,
  type CardDiff,
  type CardEvidence,
  type CardRevision,
  type CardSource,
  type CardStatus,
  type CardType,
} from '../api/cards'
import { p5GraphApi, type P5CardGraphDetail } from '../api/p5graph'
import { useAuthStore } from '../stores/auth'

const props = defineProps<{
  modelValue: boolean
  cardId: string
  revisionMode?: 'published' | 'review'
  entrySource?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'changed'): void
}>()

const authStore = useAuthStore()
const router = useRouter()
const isAdmin = computed(() => authStore.user?.groups?.includes('__local_admin__') ?? false)

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const loading = ref(false)
const error = ref('')
const detail = ref<CardDetail | null>(null)
const evidences = ref<CardEvidence[]>([])
const sources = ref<CardSource[]>([])
const revisions = ref<CardRevision[]>([])
const graphDetail = ref<P5CardGraphDetail | null>(null)
const graphError = ref(false)
const diffResult = ref<CardDiff | null>(null)
const diffTarget = ref<CardRevision | null>(null)
const activeTab = ref('content')
const currentView = ref<'published' | 'review'>('published')

const BLOCK_TYPE_LABELS: Record<string, string> = {
  prerequisite: '前置条件',
  step: '操作步骤',
  parameter: '参数',
  rule: '规则',
  diagnostic: '诊断',
  warning: '注意事项',
  result: '预期结果',
  reference: '参考信息',
}
const CLAIM_TYPE_LABELS: Record<string, string> = {
  fact: '事实',
  step: '步骤',
  parameter: '参数',
  rule: '规则',
  diagnostic: '诊断',
  decision: '决策',
}
const CLAIM_TYPE_TAG: Record<string, 'primary' | 'success' | 'warning' | 'info'> = {
  fact: 'info', step: 'primary', parameter: 'success', rule: 'warning', diagnostic: 'warning', decision: 'info',
}
const ENTITY_TYPE_LABELS: Record<string, string> = {
  product: '产品', version: '版本', error_code: '错误码', parameter: '参数', component: '组件',
  software: '软件', tool: '工具', system: '系统', artifact: '制品', operation: '操作', solution: '解决方案',
}
const RELATION_LABELS: Record<string, string> = {
  belongs_to: '属于', contains: '包含', installed_on: '安装在',
  connects_to: '连接到', requires: '依赖', applies_to: '适用于',
  supports: '支持', leads_to: '导致', solves: '解决',
  supersedes: '替代', uploaded_to: '上传到', runs_on: '运行于',
  deployed_to: '部署到', invokes: '调用', uses: '使用',
  stored_in: '保存到', flashed_to: '烧录到',
}
const TYPE_LABELS: Record<CardType, string> = { guide: '指南', reference: '参考', decision: '决策' }
const TYPE_COLORS: Record<CardType, 'primary' | 'success' | 'info'> = { guide: 'primary', reference: 'success', decision: 'info' }
const STATUS_MAP: Record<CardStatus, { label: string; type: 'info' | 'warning' | 'success' | 'danger' }> = {
  draft: { label: '草稿', type: 'info' },
  pending: { label: '待审', type: 'warning' },
  published: { label: '已发布', type: 'success' },
  rejected: { label: '已驳回', type: 'danger' },
  archived: { label: '已归档', type: 'info' },
}

const typeLabel = computed(() => TYPE_LABELS[detail.value?.card_type || 'guide'])
const typeColor = computed(() => TYPE_COLORS[detail.value?.card_type || 'guide'])
const statusLabel = computed(() => STATUS_MAP[detail.value?.status || 'draft']?.label ?? detail.value?.status)
const statusColor = computed(() => STATUS_MAP[detail.value?.status || 'draft']?.type ?? 'info')
const riskLabel = computed(() => ({ low: '低', medium: '中', high: '高' }[detail.value?.risk_level || ''] ?? ''))
const versionLabel = computed(() => currentView.value === 'review' ? '待审版本' : '线上可信版本')
const canReview = computed(() => detail.value && (
  detail.value.status === 'draft'
  || detail.value.status === 'pending'
  || Boolean(detail.value.draft_revision_id)
))
const canReject = computed(() => detail.value && (
  detail.value.status === 'draft'
  || detail.value.status === 'pending'
  || Boolean(detail.value.draft_revision_id)
))

const graphStatusLabel = computed(() => {
  const s = detail.value?.graph_status
  return ({ unlinked: '未入图', entities_only: '仅实体', linked: '已入图' } as Record<string, string>)[s || ''] || '未知'
})
const graphStatusTag = computed(() => {
  const s = detail.value?.graph_status
  return ({ unlinked: 'warning', entities_only: 'info', linked: 'success' } as Record<string, 'warning' | 'info' | 'success'>)[s || ''] || 'info'
})

function contributionLabel(t: string | null): string {
  return ({ primary: '主来源', supporting: '支持来源', derived: '派生来源' } as Record<string, string>)[t || ''] || t || '来源'
}

function changeColor(type: string): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  return ({ NEW: 'success', ENRICH: 'primary', UPDATE: 'warning', CONFLICT: 'danger', SUPERSEDE: 'info' } as any)[type] || 'info'
}

function entrySource(): string {
  return props.entrySource || (currentView.value === 'review' ? '审核工作台' : '知识图谱/Community')
}

async function loadGraph() {
  graphDetail.value = null
  graphError.value = false
  try {
    graphDetail.value = await p5GraphApi.cardGraph(props.cardId)
  } catch {
    graphError.value = true
  }
}

async function load() {
  if (!props.cardId) return
  loading.value = true
  error.value = ''
  try {
    detail.value = await cardApi.get(props.cardId, currentView.value)
    ;[evidences.value, sources.value, revisions.value] = await Promise.all([
      cardApi.evidence(props.cardId),
      cardApi.sources(props.cardId),
      cardApi.revisions(props.cardId),
    ])
    diffResult.value = null
    diffTarget.value = null
    activeTab.value = 'content'
    await loadGraph()
  } catch (e: any) {
    const status = e?.response?.status
    // 记录错误上下文：card_id、入口来源、HTTP 状态（不含正文/Token）。
    console.error('[CardDetail] 加载失败', { card_id: props.cardId, entry: entrySource(), status })
    if (status === 404) {
      error.value = '该 Card 不存在或无权访问'
    } else if (status === 500) {
      error.value = 'Card 内容加载失败'
    } else {
      error.value = e?.response?.data?.detail || 'Card 内容加载失败'
    }
  } finally {
    loading.value = false
  }
}

function toggleRevisionMode() {
  currentView.value = currentView.value === 'published' ? 'review' : 'published'
  load()
}

function openSource(s: CardSource) {
  if (s.source_url) {
    window.open(s.source_url, '_blank', 'noopener,noreferrer')
  } else if (s.page_id) {
    router.push({ path: '/knowledge/documents' })
  }
}

watch(
  () => [props.modelValue, props.cardId],
  ([open]) => {
    if (open) {
      currentView.value = props.revisionMode || 'published'
      load()
    }
  },
)

watch(
  () => props.revisionMode,
  (v) => {
    currentView.value = v || 'published'
  },
)

function onClosed() {
  detail.value = null
  evidences.value = []
  sources.value = []
  revisions.value = []
  graphDetail.value = null
  graphError.value = false
  diffResult.value = null
  error.value = ''
}

async function selectRevision(rev: CardRevision) {
  diffTarget.value = rev
  try {
    diffResult.value = await cardApi.diff(props.cardId, rev.id)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载 Diff 失败')
  }
}

const reviewing = ref(false)

async function approve() {
  if (reviewing.value) return
  reviewing.value = true
  try {
    const result = await cardApi.approveAndPublish(props.cardId)
    ElMessage.success(result.already_published ? '卡片已经发布' : '审核通过并发布')
    emit('changed')
    visible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '审核发布失败')
  } finally {
    reviewing.value = false
  }
}

async function reject() {
  try {
    await cardApi.reject(props.cardId)
    ElMessage.success('卡片已驳回')
    emit('changed')
    visible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function archive() {
  try {
    await cardApi.archive(props.cardId)
    ElMessage.success('卡片已归档')
    emit('changed')
    visible.value = false
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '归档失败')
  }
}
</script>

<style scoped>
.card-detail-body {
  min-height: 200px;
}
.detail-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.detail-header {
  display: flex;
  gap: 8px;
  align-items: center;
}
.confidence {
  font-size: 12px;
  color: #64748b;
  margin-left: auto;
}
.version-bar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.detail-summary {
  font-size: 13px;
  color: #4b5563;
  background: #f8fafc;
  border-radius: 6px;
  padding: 10px 12px;
  margin: 0;
}
.graph-status-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.graph-status-meta {
  font-size: 12px;
  color: #6b7280;
}
.error-box {
  padding: 32px;
  text-align: center;
}
.error-text {
  color: #dc2626;
  font-size: 14px;
  margin-bottom: 12px;
}
.tab-section-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  margin: 14px 0 8px;
  border-bottom: 1px solid #f3f4f6;
  padding-bottom: 6px;
}
.blocks-list, .evidence-list, .revisions-list, .source-list, .claims-list, .relation-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.block-item, .evidence-item, .source-item, .claim-item, .relation-item {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px;
}
.block-head, .claim-head, .evidence-head, .source-head {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}
.block-heading {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
}
.block-content, .evidence-content, .revision-body, .claim-statement {
  font-size: 13px;
  color: #4b5563;
  white-space: pre-wrap;
}
.revision-body {
  background: #f8fafc;
  border-radius: 6px;
  padding: 10px 12px;
}
.claim-meta {
  font-size: 12px;
  color: #6b7280;
}
.source-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  flex: 1;
}
.source-meta {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.observation-row {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  font-size: 12px;
  color: #6b7280;
  margin-top: 6px;
}
.relation-item {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  color: #374151;
}
.tag-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.revision-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  cursor: pointer;
}
.revision-meta {
  font-size: 12px;
  color: #6b7280;
  flex: 1;
}
.diff-box {
  margin-top: 10px;
  border: 1px dashed #f59e0b;
  border-radius: 8px;
  padding: 10px;
  background: #fffbeb;
}
.diff-summary {
  font-size: 12px;
  color: #92400e;
  margin-bottom: 8px;
}
.diff-detail {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}
.diff-add { color: #10b981; }
.diff-remove { color: #ef4444; }
.diff-change { color: #f59e0b; }
.placeholder {
  color: #9ca3af;
  font-size: 13px;
  text-align: center;
  padding: 20px;
}
.detail-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  border-top: 1px solid #f3f4f6;
  padding-top: 12px;
}
</style>
