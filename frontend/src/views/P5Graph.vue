<template>
  <div class="p5-graph-page">
    <header class="p5-header">
      <div class="p5-stats">
        <span>{{ stats.entity_count }} 个实体</span>
        <span>{{ stats.relation_count }} 条关系</span>
        <span class="coverage-stat">
          已发布 {{ stats.published_card_count }} / 入图 {{ stats.linked_card_count }}
          <el-link
            v-if="stats.unlinked_card_count > 0"
            type="warning"
            :underline="false"
            @click="openUnlinked"
          >
            {{ stats.unlinked_card_count }} 张未入图
          </el-link>
        </span>
        <span v-if="truncated" class="truncated-tip">（超出视图上限，已截断）</span>
      </div>
      <div class="p5-controls">
        <el-radio-group v-model="viewMode" size="small">
          <el-radio-button value="entity">实体关系</el-radio-button>
          <el-radio-button value="card">Card 图谱</el-radio-button>
        </el-radio-group>
        <el-input v-model="filterText" placeholder="过滤实体..." clearable style="width: 180px" />
        <el-button v-if="isAdmin" type="primary" @click="rebuild" :loading="rebuilding">重建图谱</el-button>
      </div>
    </header>

    <div class="p5-body">
      <EntityGraphCanvas
        :entities="filteredEntities"
        :relations="visibleRelations"
        :cards="canvasCards"
        :card-entity-links="canvasCardLinks"
        :filter-text="filterText"
        @select="openEntity"
        @select-card="openCard"
      />
    </div>

    <!-- 实体详情 Drawer -->
    <el-drawer v-model="detailVisible" :title="detail?.name || '实体详情'" size="420px">
      <template v-if="detail">
        <div class="detail-meta">
          <el-tag :type="TYPE_TAG[detail.entity_type] || 'info'" size="small">{{ TYPE_LABELS[detail.entity_type] || detail.entity_type }}</el-tag>
          <span class="detail-name">{{ detail.name }}</span>
          <el-tag v-if="detail.disambiguation_status !== 'confirmed'" size="small" type="warning">
            {{ DISAMBIGUATION_LABELS[detail.disambiguation_status] || detail.disambiguation_status }}
          </el-tag>
        </div>
        <div class="detail-normalized">归一化：{{ detail.normalized }}</div>

        <h4 class="detail-section">别名（{{ detail.aliases.length }}）</h4>
        <div v-if="detail.aliases.length" class="alias-list">
          <el-tag v-for="a in detail.aliases" :key="a" size="small" type="info">{{ a }}</el-tag>
        </div>
        <div v-else class="empty-tip">无别名</div>

        <h4 class="detail-section">关联卡片（{{ detail.cards.length }}）</h4>
        <div v-if="detail.cards.length" class="card-list">
          <div
            v-for="c in detail.cards"
            :key="c.id"
            class="card-item"
            @click="openCard(c)"
          >
            {{ c.canonical_title }}
          </div>
        </div>
        <div v-else class="empty-tip">无关联卡片</div>
      </template>
    </el-drawer>

    <!-- 未入图 Card Drawer -->
    <el-drawer v-model="unlinkedVisible" title="已发布但未入图的知识" size="420px">
      <div v-loading="unlinkedLoading" class="unlinked-list">
        <div v-if="!unlinkedCards.length && !unlinkedLoading" class="empty-tip">
          所有已发布卡片均已进入图谱。
        </div>
        <div v-for="c in unlinkedCards" :key="c.id" class="card-item unlinked-item" @click="openCard(c)">
          <el-tag size="small" :type="cardTypeTag(c.card_type)">{{ cardTypeLabel(c.card_type) }}</el-tag>
          <span class="unlinked-title">{{ c.canonical_title }}</span>
        </div>
      </div>
    </el-drawer>

    <!-- Card 详情 Drawer -->
    <CardDetailDrawer
      v-model="cardDetailVisible"
      :card-id="cardDetailId"
      revision-mode="published"
      @changed="load"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  p5GraphApi,
  type P5CardEntityLink,
  type P5CardNode,
  type P5Entity,
  type P5GraphStats,
  type P5Relation,
} from '../api/p5graph'
import { useAuthStore } from '../stores/auth'
import EntityGraphCanvas from '../components/graph/EntityGraphCanvas.vue'
import CardDetailDrawer from '../components/CardDetailDrawer.vue'

const authStore = useAuthStore()
const isAdmin = computed(() => authStore.user?.groups?.includes('__local_admin__') ?? false)

const TYPE_LABELS: Record<string, string> = {
  product: '产品', version: '版本', error_code: '错误码',
  solution: '解决方案', parameter: '参数', component: '组件',
  software: '软件', tool: '工具', system: '系统',
  artifact: '制品', operation: '操作',
}
const TYPE_TAG: Record<string, 'danger' | 'primary' | 'warning' | 'success' | 'info'> = {
  product: 'danger', version: 'primary', error_code: 'warning',
  solution: 'success', parameter: 'info', component: 'info',
  software: 'primary', tool: 'success', system: 'info',
  artifact: 'warning', operation: 'primary',
}
const DISAMBIGUATION_LABELS: Record<string, string> = {
  confirmed: '已确认', pending: '待消歧', manual_review: '待人工',
}

const CARD_TYPE_LABELS: Record<string, string> = { guide: '指南', reference: '参考', decision: '决策' }
const CARD_TYPE_TAGS: Record<string, 'primary' | 'success' | 'info'> = {
  guide: 'primary', reference: 'success', decision: 'info',
}

const entities = ref<P5Entity[]>([])
const relations = ref<P5Relation[]>([])
const cards = ref<P5CardNode[]>([])
const cardEntityLinks = ref<P5CardEntityLink[]>([])
const stats = ref<P5GraphStats>({
  entity_count: 0, relation_count: 0,
  published_card_count: 0, linked_card_count: 0, unlinked_card_count: 0,
})
const truncated = ref(false)
const filterText = ref('')
const rebuilding = ref(false)
const viewMode = ref<'entity' | 'card'>('entity')

const detailVisible = ref(false)
const detail = ref<P5Entity | null>(null)

const unlinkedVisible = ref(false)
const unlinkedLoading = ref(false)
const unlinkedCards = ref<P5CardNode[]>([])

const cardDetailVisible = ref(false)
const cardDetailId = ref('')

const filteredEntities = computed(() => {
  if (!filterText.value) return entities.value
  const q = filterText.value.toLowerCase()
  return entities.value.filter(e => e.name.toLowerCase().includes(q) || e.normalized.includes(q))
})

/** Card 视图时才传入 Card 节点与关联边。 */
const canvasCards = computed(() => (viewMode.value === 'card' ? cards.value : []))
const canvasCardLinks = computed(() => (viewMode.value === 'card' ? cardEntityLinks.value : []))

/** 只保留两端实体都在返回集内的关系，避免悬空边。 */
const visibleRelations = computed(() => {
  const ids = new Set(entities.value.map(e => e.id))
  return relations.value.filter(r => ids.has(r.source_entity_id) && ids.has(r.target_entity_id))
})

function cardTypeLabel(t: string): string {
  return CARD_TYPE_LABELS[t] || t
}
function cardTypeTag(t: string): 'primary' | 'success' | 'info' {
  return CARD_TYPE_TAGS[t] || 'info'
}

async function load() {
  try {
    const data = await p5GraphApi.graph()
    entities.value = data.entities
    relations.value = data.relations
    cards.value = data.cards
    cardEntityLinks.value = data.card_entity_links
    stats.value = data.stats
    truncated.value = data.truncated
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载图谱失败')
  }
}

function openEntity(e: P5Entity) {
  detail.value = e
  detailVisible.value = true
}

function openCard(c: P5CardNode) {
  cardDetailId.value = c.id
  cardDetailVisible.value = true
}

async function openUnlinked() {
  unlinkedVisible.value = true
  unlinkedLoading.value = true
  try {
    unlinkedCards.value = await p5GraphApi.unlinkedCards()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载未入图卡片失败')
  } finally {
    unlinkedLoading.value = false
  }
}

async function rebuild() {
  rebuilding.value = true
  try {
    const res = await p5GraphApi.rebuild()
    ElMessage.success(res.message)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重建失败')
  } finally {
    rebuilding.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.p5-graph-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.p5-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.p5-stats {
  font-size: 13px;
  color: #6b7280;
  display: flex;
  gap: 16px;
  align-items: center;
}
.coverage-stat {
  color: #475569;
}
.truncated-tip {
  color: #b45309;
}
.p5-controls {
  display: flex;
  gap: 8px;
  align-items: center;
}
.p5-body {
  flex: 1;
  overflow: hidden;
}
.empty-tip {
  font-size: 13px;
  color: #9ca3af;
  padding: 16px;
  text-align: center;
}
.detail-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.detail-name {
  font-size: 16px;
  font-weight: 600;
  color: #111827;
}
.detail-normalized {
  font-size: 12px;
  color: #9ca3af;
  margin-bottom: 8px;
}
.detail-section {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
  margin: 14px 0 8px;
  border-bottom: 1px solid #f3f4f6;
  padding-bottom: 6px;
}
.alias-list {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.card-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.card-item {
  font-size: 13px;
  color: #374151;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 6px;
}
.card-item:hover {
  background: #f3f4f6;
}
.unlinked-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.unlinked-item {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid #e5e7eb;
  padding: 8px 10px;
}
.unlinked-title {
  flex: 1;
}
</style>
