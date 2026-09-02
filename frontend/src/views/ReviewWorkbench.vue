<template>
  <div class="review-container">
    <header class="review-header">
      <div class="filter-bar">
        <el-select v-model="statusFilter" placeholder="按状态过滤" clearable style="width: 200px" @change="loadCards">
          <el-option label="待审队列" value="queue" />
          <el-option label="草稿" value="draft" />
          <el-option label="待审" value="pending" />
          <el-option label="已发布" value="published" />
          <el-option label="已驳回" value="rejected" />
          <el-option label="已归档" value="archived" />
        </el-select>
        <el-select v-model="typeFilter" placeholder="按类型过滤" clearable style="width: 160px" @change="loadCards">
          <el-option label="指南" value="guide" />
          <el-option label="参考" value="reference" />
          <el-option label="决策" value="decision" />
        </el-select>
        <el-button @click="loadCards">刷新</el-button>
        <el-button
          v-if="isAdmin && batchIds.length"
          type="success"
          :loading="batchLoading"
          @click="batchPublishLowRisk"
        >
          批量发布低风险（{{ batchIds.length }}）
        </el-button>
      </div>
      <div class="info-bar">
        <span>共 {{ totalCount }} 张卡片</span>
        <span v-if="statusFilter === 'queue'">按文档 / 编译批次分组</span>
      </div>
    </header>

    <div class="review-body">
      <div v-loading="loading" class="card-list-scroll">
        <div v-if="!totalCount && !loading" class="placeholder">无卡片</div>

        <template v-if="statusFilter === 'queue'">
          <section v-for="group in groups" :key="group.compile_run_id + group.source_page_id" class="review-group">
            <header class="group-head">
              <strong>{{ group.page_title || '未关联文档' }}</strong>
              <span>{{ group.cards.length }} 张 · 可批量 {{ group.low_risk_ids.length }}</span>
            </header>
            <CardCard
              v-for="card in visibleGroupCards(group)"
              :key="card.id"
              :card="card"
              :change-type="card.change_type"
              :debt-count="0"
              :is-admin="isAdmin"
              :reviewing="reviewingIds.has(card.id)"
              @view="openDetail(card.id)"
              @approve="onApprove(card.id)"
              @reject="onReject(card.id)"
            />
            <p v-if="group.cards.some(c => c.requires_individual_review)" class="group-note">
              高风险 / 冲突 / 可能重复须单独审核，不进入批量发布。
            </p>
          </section>
        </template>

        <CardCard
          v-else
          v-for="card in cards"
          :key="card.id"
          :card="card"
          :change-type="card.status === 'draft' ? 'NEW' : null"
          :debt-count="debtCounts[card.id] || 0"
          :is-admin="isAdmin"
          :reviewing="reviewingIds.has(card.id)"
          @view="openDetail(card.id)"
          @approve="onApprove(card.id)"
          @reject="onReject(card.id)"
        />
      </div>
    </div>

    <CardDetailDrawer
      v-model="detailVisible"
      :card-id="detailCardId"
      revision-mode="review"
      @changed="loadCards"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { cardApi, type KnowledgeCard, type ReviewQueueGroup } from '../api/cards'
import { useAuthStore } from '../stores/auth'
import CardCard from '../components/CardCard.vue'
import CardDetailDrawer from '../components/CardDetailDrawer.vue'

const authStore = useAuthStore()
const isAdmin = computed(() => authStore.user?.groups?.includes('__local_admin__') ?? false)

const cards = ref<KnowledgeCard[]>([])
const groups = ref<ReviewQueueGroup[]>([])
const loading = ref(false)
const statusFilter = ref<string | undefined>('queue')
const typeFilter = ref<string | undefined>(undefined)

const detailVisible = ref(false)
const detailCardId = ref('')

const debtCounts = reactive<Record<string, number>>({})
const batchLoading = ref(false)
const reviewingIds = ref(new Set<string>())

const totalCount = computed(() =>
  statusFilter.value === 'queue'
    ? groups.value.reduce((sum, group) => sum + group.cards.length, 0)
    : cards.value.length
)

const batchIds = computed(() =>
  groups.value.flatMap(group => group.low_risk_ids)
)

function visibleGroupCards(group: ReviewQueueGroup) {
  if (!typeFilter.value) return group.cards
  return group.cards.filter(card => card.card_type === typeFilter.value)
}

async function loadCards() {
  loading.value = true
  try {
    if (statusFilter.value === 'queue' || !statusFilter.value) {
      const queue = await cardApi.reviewQueue()
      groups.value = queue.groups
      cards.value = []
    } else {
      cards.value = await cardApi.list({
        status: statusFilter.value,
        card_type: typeFilter.value,
        limit: 100,
      })
      groups.value = []
    }
    Object.keys(debtCounts).forEach(k => delete debtCounts[k])
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

function openDetail(cardId: string) {
  detailCardId.value = cardId
  detailVisible.value = true
}

async function onApprove(cardId: string) {
  if (reviewingIds.value.has(cardId)) return

  reviewingIds.value.add(cardId)
  try {
    const result = await cardApi.approveAndPublish(cardId)
    ElMessage.success(result.already_published ? '卡片已经发布' : '审核通过并发布')
    await loadCards()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '审核发布失败')
  } finally {
    reviewingIds.value.delete(cardId)
  }
}

async function onReject(cardId: string) {
  try {
    await cardApi.reject(cardId)
    ElMessage.success('已驳回')
    await loadCards()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function batchPublishLowRisk() {
  if (!batchIds.value.length) return
  batchLoading.value = true
  try {
    const result = await cardApi.batchPublish(batchIds.value)
    ElMessage.success(`已发布 ${result.published.length} 张，跳过 ${result.skipped.length} 张`)
    await loadCards()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '批量发布失败')
  } finally {
    batchLoading.value = false
  }
}

onMounted(loadCards)
</script>

<style scoped>
.review-container {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.review-header {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.filter-bar {
  display: flex;
  gap: 8px;
  align-items: center;
}
.info-bar {
  font-size: 13px;
  color: #6b7280;
  display: flex;
  gap: 24px;
}
.review-body {
  flex: 1;
  overflow: hidden;
}
.card-list-scroll {
  height: 100%;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-right: 4px;
}
.review-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fafafa;
}
.group-head {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: #374151;
}
.group-note {
  margin: 0;
  font-size: 12px;
  color: #b45309;
}
.placeholder {
  color: #9ca3af;
  font-size: 13px;
  padding: 24px;
  text-align: center;
}
</style>
