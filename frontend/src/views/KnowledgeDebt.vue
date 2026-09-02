<template>
  <div class="debt-container">
    <header class="debt-header">
      <el-radio-group v-model="statusFilter" @change="loadDebts">
        <el-radio-button value="open">待处理</el-radio-button>
        <el-radio-button value="resolved">已处理</el-radio-button>
      </el-radio-group>
      <el-select v-model="typeFilter" placeholder="类型" clearable style="width: 160px" @change="loadDebts">
        <el-option v-for="t in TYPE_OPTIONS" :key="t.value" :label="t.label" :value="t.value" />
      </el-select>
      <el-button type="primary" :loading="scanning" @click="handleScan">扫描缺口</el-button>
    </header>

    <div class="debt-body">
      <div v-if="loading" v-loading="true" style="min-height: 200px"></div>
      <div v-else-if="!debts.length" class="placeholder">无债务记录</div>
      <div v-else class="debt-list-scroll">
        <div v-for="debt in debts" :key="debt.id" class="debt-item">
          <div class="debt-item-header">
            <el-tag :type="TYPE_TAG[debt.debt_type] || 'info'" size="small">
              {{ TYPE_LABELS[debt.debt_type] || debt.debt_type }}
            </el-tag>
            <!-- P3-FE-07：根因 + 优先级 + 出现次数 -->
            <el-tag v-if="debt.root_cause" :type="ROOT_CAUSE_TAG[debt.root_cause] || 'info'" size="small">
              {{ ROOT_CAUSE_LABELS[debt.root_cause] || debt.root_cause }}
            </el-tag>
            <el-tag v-if="debt.priority" type="danger" size="small" effect="dark">{{ debt.priority }}</el-tag>
            <span class="debt-score">严重度 {{ debt.score }} · 出现 {{ debt.occurrence_count }} 次</span>
          </div>
          <div class="debt-description">{{ debt.title || debt.description }}</div>
          <div v-if="debt.related_question" class="debt-question">相关问题：{{ debt.related_question }}</div>
          <div v-if="debt.resolution_card_id" class="debt-card-link">
            关联卡片：{{ debt.resolution_card_id }}
          </div>
          <footer class="debt-actions">
            <el-button size="small" type="success" @click="linkCardDialog(debt)">关联卡片</el-button>
            <el-button v-if="debt.status === 'open'" size="small" @click="revalidate(debt)">重新验证</el-button>
            <el-button v-if="debt.status === 'open'" size="small" @click="resolveDebt(debt)">标记处理</el-button>
          </footer>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { debtApi, type Debt } from '../api/debts'

const TYPE_LABELS: Record<string, string> = {
  no_answer: '高频无答案', low_score: '用户反馈差', expired: '过期未更新',
  no_evidence: '缺证据',
  missing_knowledge: '缺知识', missing_evidence: '缺证据', conflict: '冲突',
  outdated: '过时', missing_visual: '缺图片',
}
const TYPE_TAG: Record<string, 'danger' | 'warning' | 'info' | 'success'> = {
  no_answer: 'danger', low_score: 'warning', expired: 'warning',
  no_evidence: 'info',
  missing_knowledge: 'danger', missing_evidence: 'warning', conflict: 'danger',
  outdated: 'warning', missing_visual: 'warning',
}
const TYPE_OPTIONS = Object.entries(TYPE_LABELS).map(([value, label]) => ({ value, label }))

// P3-FE-07：根因标签
const ROOT_CAUSE_LABELS: Record<string, string> = {
  missing_knowledge: '缺知识', missing_evidence: '缺证据', conflict: '冲突',
  outdated: '过时', missing_visual: '缺图片', retrieval_failure: '召回失败',
  index_failure: '索引不一致', system_failure: '系统故障', ambiguous_query: '问题歧义',
}
const ROOT_CAUSE_TAG: Record<string, 'danger' | 'warning' | 'info' | 'success'> = {
  missing_knowledge: 'danger', missing_evidence: 'warning', conflict: 'danger',
  outdated: 'warning', missing_visual: 'warning', retrieval_failure: 'info',
  index_failure: 'info', system_failure: 'info', ambiguous_query: 'info',
}

const statusFilter = ref('open')
const typeFilter = ref('')
const debts = ref<Debt[]>([])
const loading = ref(false)
const scanning = ref(false)

async function loadDebts() {
  loading.value = true
  try {
    debts.value = await debtApi.list({
      status: statusFilter.value,
      debt_type: typeFilter.value || undefined,
      limit: 100,
    })
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

async function handleScan() {
  scanning.value = true
  try {
    const res = await debtApi.scan()
    ElMessage.success(res.message)
    await loadDebts()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '扫描失败')
  } finally {
    scanning.value = false
  }
}

// P3-FE-07：关联可解决该债务的 Card
async function linkCardDialog(debt: Debt) {
  try {
    const { value } = await ElMessageBox.prompt('输入可解决该债务的卡片 ID', '关联卡片', {
      inputValue: debt.resolution_card_id || '',
    })
    await debtApi.linkCard(debt.id, value.trim())
    ElMessage.success('已关联卡片')
    await loadDebts()
  } catch {
    // 取消
  }
}

// P3-FE：重新验证（自动关闭闭环）
async function revalidate(debt: Debt) {
  try {
    const res = await debtApi.revalidate(debt.id)
    if (res.resolved) {
      ElMessage.success('关联卡片已发布，债务自动关闭')
    } else {
      ElMessage.info(res.message)
    }
    await loadDebts()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '重新验证失败')
  }
}

async function resolveDebt(debt: Debt) {
  try {
    const res = await debtApi.resolve(debt.id)
    ElMessage.success(res.message)
    await loadDebts()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

onMounted(() => loadDebts())
</script>

<style scoped>
.debt-container {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.debt-header {
  display: flex;
  gap: 12px;
  align-items: center;
}
.debt-body {
  flex: 1;
  overflow-y: auto;
}
.debt-list-scroll {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.debt-item {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
}
.debt-item-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.debt-score {
  font-size: 12px;
  color: #64748b;
  font-weight: 600;
}
.debt-description {
  font-size: 14px;
  color: #111827;
  margin: 8px 0 4px;
  font-weight: 500;
}
.debt-question {
  font-size: 12px;
  color: #6b7280;
}
.debt-card-link {
  font-size: 12px;
  color: #059669;
  margin-top: 4px;
}
.debt-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 10px;
  border-top: 1px solid #f3f4f6;
  padding-top: 8px;
}
.placeholder {
  color: #9ca3af;
  font-size: 13px;
  padding: 40px;
  text-align: center;
}
</style>
