<template>
  <div class="health-container">
    <header class="health-header">
      <h2>知识质量</h2>
      <el-button :loading="loading" @click="loadHealth">刷新</el-button>
    </header>

    <div class="health-cards">
      <div
        v-for="card in cards"
        :key="card.key"
        class="health-card"
        :class="[`health-card--${card.tone}`, { active: selectedKey === card.key }]"
        @click="selectedKey = card.key"
      >
        <div class="card-value">{{ card.value }}</div>
        <div class="card-label">{{ card.label }}</div>
        <div v-if="card.note" class="card-note">{{ card.note }}</div>
      </div>
    </div>

    <div v-if="selectedMetric" class="health-drilldown">
      <h3>{{ selectedMetric.label }}</h3>
      <p class="definition">{{ selectedMetric.definition }}</p>
      <div v-if="selectedMetric.sampleIds.length" class="sample-ids">
        <el-tag v-for="id in selectedMetric.sampleIds" :key="id" size="small">{{ id }}</el-tag>
      </div>
      <div v-else class="placeholder">暂无可下钻对象</div>
    </div>

    <div v-if="!loading" class="health-summary">
      <el-alert :type="summaryTone" :title="summaryText" show-icon :closable="false" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import http from '../api/http'

interface QualityMetric {
  count?: number
  ratio?: number
  definition?: string
  sample_ids?: string[]
  published_claims?: number
  claims_with_evidence?: number
}

const loading = ref(false)
const quality = ref<Record<string, any>>({})
const selectedKey = ref('draft_backlog')

interface HealthCard {
  key: string
  label: string
  value: string | number
  tone: 'primary' | 'success' | 'warning' | 'danger' | 'info'
  note?: string
  definition: string
  sampleIds: string[]
}

function metric(key: string): QualityMetric {
  const value = quality.value[key]
  return value && typeof value === 'object' ? value : {}
}

const cards = computed<HealthCard[]>(() => {
  const coverage = metric('evidence_coverage')
  const items: HealthCard[] = [
    {
      key: 'draft_backlog',
      label: 'Card 草稿积压',
      value: metric('draft_backlog').count || 0,
      tone: 'info',
      definition: metric('draft_backlog').definition || '',
      sampleIds: metric('draft_backlog').sample_ids || [],
    },
    {
      key: 'evidence_coverage',
      label: 'Evidence 覆盖率',
      value: `${Math.round((coverage.ratio ?? 1) * 100)}%`,
      tone: 'success',
      note: `${coverage.claims_with_evidence || 0}/${coverage.published_claims || 0} Claims`,
      definition: coverage.definition || '',
      sampleIds: coverage.sample_ids || [],
    },
    {
      key: 'duplicate_rate',
      label: '重复候选率',
      value: `${Math.round((metric('duplicate_rate').ratio || 0) * 100)}%`,
      tone: 'warning',
      note: `${metric('duplicate_rate').count || 0} 条`,
      definition: metric('duplicate_rate').definition || '',
      sampleIds: metric('duplicate_rate').sample_ids || [],
    },
    {
      key: 'conflict_rate',
      label: '冲突率',
      value: metric('conflict_rate').count || 0,
      tone: 'danger',
      definition: metric('conflict_rate').definition || '',
      sampleIds: metric('conflict_rate').sample_ids || [],
    },
    {
      key: 'expiry_rate',
      label: '过期率',
      value: metric('expiry_rate').count || 0,
      tone: 'warning',
      definition: metric('expiry_rate').definition || '',
      sampleIds: metric('expiry_rate').sample_ids || [],
    },
    {
      key: 'visual_pending',
      label: 'visual_pending',
      value: metric('visual_pending').count || 0,
      tone: 'warning',
      definition: metric('visual_pending').definition || '',
      sampleIds: metric('visual_pending').sample_ids || [],
    },
    {
      key: 'community_dirty',
      label: 'Community dirty',
      value: metric('community_dirty').count || 0,
      tone: 'info',
      definition: metric('community_dirty').definition || '',
      sampleIds: metric('community_dirty').sample_ids || [],
    },
    {
      key: 'wiki_dirty',
      label: 'Wiki dirty',
      value: metric('wiki_dirty').count || 0,
      tone: 'info',
      definition: metric('wiki_dirty').definition || '',
      sampleIds: metric('wiki_dirty').sample_ids || [],
    },
    {
      key: 'wiki_orphan',
      label: 'Wiki orphan',
      value: metric('wiki_orphan').count || 0,
      tone: 'info',
      definition: metric('wiki_orphan').definition || '',
      sampleIds: metric('wiki_orphan').sample_ids || [],
    },
    {
      key: 'sync_failures',
      label: '同步失败',
      value: metric('sync_failures').count || 0,
      tone: 'danger',
      definition: metric('sync_failures').definition || '',
      sampleIds: metric('sync_failures').sample_ids || [],
    },
    {
      key: 'model_degradation',
      label: '模型降级',
      value: metric('model_degradation').count || 0,
      tone: 'warning',
      definition: metric('model_degradation').definition || '',
      sampleIds: metric('model_degradation').sample_ids || [],
    },
  ]
  return items
})

const selectedMetric = computed(() => cards.value.find(item => item.key === selectedKey.value) || cards.value[0])

const summaryTone = computed(() => {
  if ((metric('conflict_rate').count || 0) > 0) return 'warning'
  if ((metric('visual_pending').count || 0) > 0) return 'warning'
  if ((metric('sync_failures').count || 0) > 0) return 'warning'
  return 'success'
})

const summaryText = computed(() => {
  const parts: string[] = []
  if ((metric('conflict_rate').count || 0) > 0) parts.push(`${metric('conflict_rate').count} 个冲突待裁定`)
  if ((metric('visual_pending').count || 0) > 0) parts.push(`${metric('visual_pending').count} 张图片 visual_pending`)
  if ((metric('draft_backlog').count || 0) > 0) parts.push(`${metric('draft_backlog').count} 张草稿积压`)
  if (!parts.length) return 'Published Card 口径下知识库状态健康'
  return '注意：' + parts.join('，')
})

async function loadHealth() {
  loading.value = true
  try {
    const res = await http.get('/api/governance/quality')
    quality.value = res.data
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => loadHealth())
</script>

<style scoped>
.health-container {
  height: 100%;
  overflow-y: auto;
  padding: 24px;
  max-width: 1100px;
  margin: 0 auto;
}
.health-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}
.health-header h2 {
  font-size: 18px;
  color: #111827;
}
.health-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.health-card {
  border-radius: 10px;
  padding: 20px;
  border: 1px solid #e5e7eb;
  background: #fff;
  cursor: pointer;
}
.health-card.active { box-shadow: 0 0 0 2px #38bdf8 inset; }
.health-card--primary { border-top: 3px solid #3b82f6; }
.health-card--success { border-top: 3px solid #10b981; }
.health-card--warning { border-top: 3px solid #f59e0b; }
.health-card--danger { border-top: 3px solid #ef4444; }
.health-card--info { border-top: 3px solid #6b7280; }
.card-value {
  font-size: 32px;
  font-weight: 700;
  color: #111827;
}
.card-label {
  font-size: 13px;
  color: #6b7280;
  margin-top: 4px;
}
.card-note {
  font-size: 11px;
  color: #9ca3af;
  margin-top: 6px;
}
.health-drilldown {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 16px 18px;
  margin-bottom: 16px;
}
.health-drilldown h3 {
  font-size: 14px;
  margin: 0 0 8px;
}
.definition {
  font-size: 13px;
  color: #4b5563;
  margin: 0 0 10px;
}
.sample-ids {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.placeholder {
  font-size: 12px;
  color: #9ca3af;
}
.health-summary {
  margin-top: 8px;
}
</style>
