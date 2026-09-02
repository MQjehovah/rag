<template>
  <div class="conflict-container">
    <header class="conflict-header">
      <el-radio-group v-model="statusFilter" @change="loadConflicts">
        <el-radio-button value="open">待解决</el-radio-button>
        <el-radio-button value="resolved">已解决</el-radio-button>
      </el-radio-group>
      <el-button type="primary" :loading="scanning" @click="handleScan">扫描冲突</el-button>
    </header>

    <div class="conflict-body">
      <section class="conflict-list">
        <div v-if="loading" v-loading="true" style="min-height: 200px"></div>
        <div v-else-if="!tasks.length" class="placeholder">无冲突任务</div>
        <div v-else class="task-list-scroll">
          <div
            v-for="task in tasks"
            :key="task.id"
            class="task-item"
            :class="{ active: selected?.id === task.id }"
            @click="selectTask(task)"
          >
            <div class="task-title">
              <el-tag size="small" type="warning">冲突</el-tag>
              <span>{{ task.entity || task.attribute }}</span>
            </div>
            <div class="task-meta">
              {{ task.scope_product || '-' }} {{ task.scope_version || '' }} {{ task.scope_region || '' }}
              · {{ (task.claims || []).length }} 条 Claim
            </div>
          </div>
        </div>
      </section>

      <section class="conflict-detail">
        <div v-if="!selected" class="placeholder">选中左侧冲突，对比 Published Card / Claim / Evidence</div>
        <template v-else>
          <h3 class="detail-title">{{ selected.entity }} · {{ selected.attribute }}</h3>
          <div class="candidate-list">
            <div v-for="claim in selected.claims" :key="claim.id" class="candidate-card">
              <div class="candidate-header">
                <el-radio v-model="keepClaimId" :value="claim.id">
                  {{ claim.status === 'superseded' ? '（已废弃）' : '保留此 Claim' }}
                </el-radio>
                <el-tag size="small" :type="claim.status === 'active' ? 'success' : 'info'">{{ claim.status }}</el-tag>
              </div>
              <div class="candidate-title">{{ claim.card_title }}</div>
              <div class="candidate-body">{{ claim.statement }}</div>
              <div class="candidate-meta">
                Card {{ claim.card_id }} · 来源 {{ claim.source_title || claim.source_page_id || '-' }}
                · {{ claim.scope?.product || '-' }} {{ claim.scope?.version || '' }} {{ claim.scope?.region || '' }}
              </div>
              <div v-if="claim.evidence?.length" class="evidence-list">
                <div v-for="ev in claim.evidence" :key="ev.id" class="evidence-item">
                  Evidence：{{ ev.content }}
                </div>
              </div>
            </div>
          </div>
          <footer class="resolve-footer">
            <el-button type="primary" :disabled="!keepClaimId" :loading="resolving" @click="handleResolve">
              保留所选 Claim，为失败方生成新 Revision
            </el-button>
          </footer>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import http from '../api/http'

interface ConflictEvidence {
  id: string
  content: string
  status: string
}
interface ConflictClaim {
  id: string
  card_id: string
  card_title: string
  claim_type: string
  statement: string
  status: string
  scope: { product?: string; version?: string; region?: string }
  evidence: ConflictEvidence[]
  source_page_id: string | null
  source_title: string
}
interface ConflictTask {
  id: string
  attribute: string
  entity: string
  scope_product: string
  scope_version: string
  scope_region: string
  status: string
  claims: ConflictClaim[]
}

const statusFilter = ref('open')
const tasks = ref<ConflictTask[]>([])
const selected = ref<ConflictTask | null>(null)
const keepClaimId = ref('')
const loading = ref(false)
const scanning = ref(false)
const resolving = ref(false)

async function loadConflicts() {
  loading.value = true
  try {
    const res = await http.get('/api/conflicts', { params: { status: statusFilter.value } })
    tasks.value = res.data[0]?.conflicts || []
    if (selected.value) {
      const match = tasks.value.find(t => t.id === selected.value?.id)
      selected.value = match || null
    }
    if (!selected.value && tasks.value.length) {
      selected.value = tasks.value[0]
    }
    if (selected.value) {
      keepClaimId.value = selected.value.claims.find(c => c.status === 'active')?.id || selected.value.claims[0]?.id || ''
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

function selectTask(task: ConflictTask) {
  selected.value = task
  keepClaimId.value = task.claims.find(c => c.status === 'active')?.id || task.claims[0]?.id || ''
}

async function handleScan() {
  scanning.value = true
  try {
    const res = await http.post('/api/conflicts/scan')
    ElMessage.success(res.data.message)
    await loadConflicts()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '扫描失败')
  } finally {
    scanning.value = false
  }
}

async function handleResolve() {
  if (!selected.value || !keepClaimId.value) return
  try {
    await ElMessageBox.confirm(
      '将为失败方 Published Card 生成新 Revision（不改写历史），是否继续？',
      '确认解决冲突',
      { type: 'warning' },
    )
  } catch {
    return
  }
  resolving.value = true
  try {
    const res = await http.post(`/api/conflicts/${selected.value.id}/resolve`, {
      keep_claim_id: keepClaimId.value,
    })
    ElMessage.success(res.data.message)
    await loadConflicts()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '解决失败')
  } finally {
    resolving.value = false
  }
}

onMounted(() => loadConflicts())
</script>

<style scoped>
.conflict-container {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.conflict-header {
  display: flex;
  gap: 12px;
  align-items: center;
}
.conflict-body {
  flex: 1;
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  overflow: hidden;
}
.conflict-list, .conflict-detail {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.task-list-scroll {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.task-item {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.task-item:hover { border-color: #38bdf8; }
.task-item.active { border-color: #38bdf8; background: #f0f9ff; }
.task-title {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  font-weight: 600;
  color: #374151;
}
.task-meta {
  font-size: 12px;
  color: #6b7280;
  margin-top: 4px;
}
.placeholder {
  color: #9ca3af;
  font-size: 13px;
  padding: 40px;
  text-align: center;
}
.detail-title {
  font-size: 14px;
  font-weight: 600;
  color: #374151;
  margin-bottom: 12px;
}
.candidate-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.candidate-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px 12px;
}
.candidate-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.candidate-title {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
  margin: 6px 0 4px;
}
.candidate-body {
  font-size: 12px;
  color: #6b7280;
}
.candidate-meta {
  font-size: 11px;
  color: #9ca3af;
  margin-top: 6px;
}
.evidence-list {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.evidence-item {
  font-size: 12px;
  color: #4b5563;
  background: #f9fafb;
  border-radius: 6px;
  padding: 6px 8px;
}
.resolve-footer {
  border-top: 1px solid #f3f4f6;
  padding-top: 10px;
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
}
</style>
