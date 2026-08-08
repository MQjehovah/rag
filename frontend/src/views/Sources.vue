<template>
  <div class="sources-page">
    <div class="sources-header">
      <h2>数据源接入</h2>
      <p>企业系统作为插件接入，LLM 分析原始内容后自动形成笔记并编译进 Wiki</p>
    </div>
    <div v-if="loading" class="sources-loading">加载中...</div>
    <div v-else class="source-list">
      <div v-for="s in sources" :key="s.key" class="source-card">
        <div class="source-card-head">
          <div>
            <div class="source-name">{{ s.name }}</div>
            <div class="source-desc">{{ s.description }}</div>
          </div>
          <el-tag :type="s.enabled ? 'success' : 'info'" size="small">
            {{ s.enabled ? '已启用' : '未启用' }}
          </el-tag>
        </div>
        <div class="source-config">配置：{{ s.config || '-' }}</div>
        <div v-if="s.status && s.status.message" class="source-status">
          {{ s.status.message }}
          <el-progress
            v-if="s.status.running && s.status.total > 0"
            :percentage="Math.round(s.status.processed / s.status.total * 100)"
            :format="() => `${s.status.processed}/${s.status.total}`"
            style="margin-top: 6px"
          />
        </div>
        <div class="source-actions">
          <el-button size="small" :loading="testing === s.key" @click="testSource(s)">测试连接</el-button>
          <template v-if="s.key === 'jira'">
            <el-select v-model="syncScope" size="small" style="width: 170px">
              <el-option
                v-for="o in scopeOptions"
                :key="o.value"
                :label="o.label"
                :value="o.value"
              />
            </el-select>
          </template>
          <el-button
            size="small"
            type="primary"
            :disabled="!s.enabled || s.status?.running"
            :loading="s.status?.running"
            @click="syncSource(s)"
          >{{ s.status?.running ? '同步中...' : '立即同步' }}</el-button>
          <el-button
            v-if="s.status?.running"
            size="small"
            type="danger"
            plain
            @click="cancelSync(s)"
          >取消同步</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import http from '../api/http'

const sources = ref<any[]>([])
const loading = ref(false)
const testing = ref('')
const syncScope = ref('incremental')
const scopeOptions = [
  { value: 'incremental', label: '增量（仅新增/变更）' },
  { value: '7', label: '回填最近 7 天' },
  { value: '30', label: '回填最近 30 天' },
  { value: '90', label: '回填最近 90 天' },
]
let timer: number | null = null

const load = async () => {
  loading.value = true
  try {
    const res = await http.get('/api/sources')
    sources.value = res.data.sources || []
  } catch { /* ignore */ } finally {
    loading.value = false
  }
}

const poll = async () => {
  try {
    const res = await http.get('/api/sources')
    sources.value = res.data.sources || []
    const anyRunning = sources.value.some(s => s.status?.running)
    if (!anyRunning && timer) {
      clearInterval(timer)
      timer = null
    }
  } catch { /* ignore */ }
}

const testSource = async (s: any) => {
  testing.value = s.key
  try {
    const res = await http.post(`/api/sources/${s.key}/test`)
    if (res.data.ok) ElMessage.success(res.data.message)
    else ElMessage.error(res.data.message)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '测试失败')
  } finally {
    testing.value = ''
  }
}

const syncSource = async (s: any) => {
  const isBackfill = syncScope.value !== 'incremental'
  const body = isBackfill
    ? { mode: 'backfill', days: Number(syncScope.value) }
    : { mode: 'incremental', days: 0 }
  try {
    const res = await http.post(`/api/sources/${s.key}/sync`, body)
    if (res.data.started) {
      ElMessage.success('同步已启动，后台进行中')
      if (!timer) timer = window.setInterval(poll, 3000)
      else poll()
    } else {
      ElMessage.info(res.data.message || '同步已在运行')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '启动同步失败')
  }
}

const cancelSync = async (s: any) => {
  try {
    const res = await http.post(`/api/sources/${s.key}/cancel`)
    ElMessage.info(res.data.message || '已请求取消')
    poll()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '取消失败')
  }
}

onMounted(() => {
  load()
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.sources-page {
  padding: 28px 40px;
  max-width: 960px;
  margin: 0 auto;
}
.sources-header h2 {
  font-size: 22px;
  color: #0f172a;
  margin-bottom: 6px;
}
.sources-header p {
  color: #64748b;
  font-size: 13px;
  margin-bottom: 20px;
}
.source-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.source-card {
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 18px 22px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.source-card-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.source-name {
  font-size: 16px;
  font-weight: 600;
  color: #1e293b;
}
.source-desc {
  font-size: 12px;
  color: #94a3b8;
  margin-top: 3px;
}
.source-config {
  font-size: 12px;
  color: #64748b;
  margin-top: 10px;
}
.source-status {
  font-size: 12px;
  color: #475569;
  background: #f8fafc;
  border-radius: 8px;
  padding: 8px 12px;
  margin-top: 10px;
}
.source-actions {
  margin-top: 14px;
  display: flex;
  gap: 8px;
}
</style>
