<template>
  <div class="card-card" :class="`card-card--${card.card_type}`">
    <header class="card-header">
      <div class="card-tags">
        <el-tag :type="typeColor" size="small" effect="dark">{{ typeLabel }}</el-tag>
        <el-tag :type="statusColor" size="small">{{ statusLabel }}</el-tag>
        <el-tag v-if="changeType" :type="changeColor" size="small">{{ changeType }}</el-tag>
        <el-tag v-if="card.risk_level" :type="riskColor" size="small">风险{{ riskLabel }}</el-tag>
        <el-tag v-if="(debtCount || 0) > 0" type="warning" size="small" effect="plain">解决 {{ debtCount || 0 }} 债务</el-tag>
      </div>
      <span class="card-confidence">{{ (card.confidence * 100).toFixed(0) }}%</span>
    </header>

    <h3 class="card-title">{{ card.canonical_title }}</h3>
    <p v-if="card.summary" class="card-summary">{{ card.summary }}</p>

    <div class="card-meta">
      <span v-if="card.scope?.product">产品：{{ card.scope.product }}</span>
      <span v-if="card.scope?.version">版本：{{ card.scope.version }}</span>
    </div>

    <footer class="card-actions">
      <el-button size="small" type="primary" @click="emit('view')">查看详情</el-button>
      <el-button
        v-if="canReview"
        size="small"
        type="success"
        :loading="reviewing"
        @click="emit('approve')"
      >
        通过并发布
      </el-button>
      <el-button v-if="canReview" size="small" type="danger" @click="emit('reject')">驳回</el-button>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { CardType, CardStatus, ChangeType, KnowledgeCard } from '../api/cards'

const props = defineProps<{
  card: KnowledgeCard
  changeType?: ChangeType | null
  debtCount?: number
  isAdmin?: boolean
  reviewing?: boolean
}>()

const emit = defineEmits<{
  (e: 'view'): void
  (e: 'approve'): void
  (e: 'reject'): void
}>()

const TYPE_LABELS: Record<CardType, string> = {
  guide: '指南',
  reference: '参考',
  decision: '决策',
}

const TYPE_COLORS: Record<CardType, 'primary' | 'success' | 'info'> = {
  guide: 'primary',
  reference: 'success',
  decision: 'info',
}

const STATUS_MAP: Record<CardStatus, { label: string; type: 'info' | 'warning' | 'success' | 'danger' }> = {
  draft: { label: '草稿', type: 'info' },
  pending: { label: '待审', type: 'warning' },
  published: { label: '已发布', type: 'success' },
  rejected: { label: '已驳回', type: 'danger' },
  archived: { label: '已归档', type: 'info' },
}

const CHANGE_COLORS: Record<string, 'primary' | 'success' | 'warning' | 'danger' | 'info'> = {
  NEW: 'success',
  ENRICH: 'primary',
  UPDATE: 'warning',
  CONFLICT: 'danger',
  SUPERSEDE: 'info',
  POSSIBLE_DUPLICATE: 'warning',
}

const typeLabel = computed(() => TYPE_LABELS[props.card.card_type])
const typeColor = computed(() => TYPE_COLORS[props.card.card_type])
const statusLabel = computed(() => STATUS_MAP[props.card.status]?.label ?? props.card.status)
const statusColor = computed(() => STATUS_MAP[props.card.status]?.type ?? 'info')
const changeColor = computed(() => CHANGE_COLORS[props.changeType || ''] ?? 'info')
const riskLabel = computed(() => ({ low: '低', medium: '中', high: '高' }[props.card.risk_level || ''] ?? ''))
const riskColor = computed(() => {
  if (props.card.risk_level === 'high') return 'danger'
  if (props.card.risk_level === 'medium') return 'warning'
  return 'success'
})
const canReview = computed(() => props.isAdmin && (props.card.status === 'draft' || props.card.status === 'pending'))
</script>

<style scoped>
.card-card {
  background: #fff;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-left-width: 4px;
}
.card-card--guide { border-left-color: #3b82f6; }
.card-card--reference { border-left-color: #10b981; }
.card-card--decision { border-left-color: #8b5cf6; }
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.card-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.card-confidence {
  font-size: 12px;
  color: #64748b;
  font-weight: 600;
}
.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
  margin: 0;
}
.card-summary {
  font-size: 13px;
  color: #6b7280;
  margin: 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.card-meta {
  font-size: 12px;
  color: #6b7280;
  display: flex;
  gap: 12px;
}
.card-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}
</style>
