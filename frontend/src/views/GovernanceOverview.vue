<template>
  <div class="gov-overview">
    <div v-loading="loading" class="overview-grid">
      <div class="stat-card" @click="go('/governance/review')">
        <div class="stat-value">{{ data.pending_cards }}</div>
        <div class="stat-label">待审核 Card</div>
      </div>
      <div class="stat-card" @click="go('/governance/review')">
        <div class="stat-value">{{ data.high_risk_changes }}</div>
        <div class="stat-label">高风险变更</div>
      </div>
      <div class="stat-card" @click="go('/governance/conflicts')">
        <div class="stat-value">{{ data.open_conflicts }}</div>
        <div class="stat-label">未解决冲突</div>
      </div>
      <div class="stat-card" @click="go('/governance/gaps')">
        <div class="stat-value">{{ data.open_debts }}</div>
        <div class="stat-label">高频知识缺口</div>
      </div>
      <div class="stat-card" @click="go('/governance/quality')">
        <div class="stat-value">{{ data.evidence_anomalies }}</div>
        <div class="stat-label">Evidence 覆盖异常</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ data.source_errors }}</div>
        <div class="stat-label">数据源同步异常</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import http from '../api/http'

const router = useRouter()
const loading = ref(false)
const data = reactive({
  pending_cards: 0,
  high_risk_changes: 0,
  open_conflicts: 0,
  open_debts: 0,
  evidence_anomalies: 0,
  source_errors: 0,
})

async function load() {
  loading.value = true
  try {
    const res = await http.get('/api/governance/overview')
    Object.assign(data, res.data)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载概览失败')
  } finally {
    loading.value = false
  }
}

function go(path: string) {
  router.push(path)
}

onMounted(load)
</script>

<style scoped>
.gov-overview {
  height: 100%;
  overflow-y: auto;
}
.overview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 16px;
}
.stat-card {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 20px;
  cursor: pointer;
  text-align: center;
  transition: box-shadow 0.2s;
}
.stat-card:hover {
  box-shadow: 0 4px 12px rgba(0,0,0,0.08);
}
.stat-value {
  font-size: 32px;
  font-weight: 700;
  color: #111827;
}
.stat-label {
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
}
</style>
