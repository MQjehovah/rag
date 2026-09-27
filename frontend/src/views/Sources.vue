<template>
  <div class="sources-page">
    <div class="page-head">
      <div>
        <h2>数据源</h2>
        <p class="muted">企业系统作为插件接入：拉取原始内容 → LLM 形成笔记 → 编译进知识库。</p>
      </div>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </div>

    <div v-if="loading && !sources.length" class="muted">加载中...</div>
    <div v-else-if="!sources.length" class="empty">暂无已接入的数据源</div>

    <div v-else class="source-list">
      <div v-for="s in sources" :key="s.key" class="source-card">
        <div class="source-icon">{{ ICONS[s.key] || '🔌' }}</div>
        <div class="source-main">
          <div class="source-card-head">
            <div class="source-title">
              <span class="source-name">{{ s.name }}</span>
              <el-tag :type="s.enabled ? 'success' : 'info'" size="small" effect="light">
                {{ s.enabled ? '已启用' : '未启用' }}
              </el-tag>
            </div>
            <div class="source-actions">
              <el-button size="small" :loading="testing === s.key" @click="testSource(s)">测试连接</el-button>
              <el-select v-if="s.key === 'jira'" v-model="syncScope" size="small" style="width: 170px">
                <el-option v-for="o in scopeOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-button
                size="small"
                type="primary"
                :disabled="!s.enabled || s.status?.running"
                :loading="s.status?.running"
                @click="syncSource(s)"
              >{{ s.status?.running ? '同步中...' : '立即同步' }}</el-button>
              <el-button v-if="s.status?.running" size="small" type="danger" plain @click="cancelSync(s)">取消</el-button>
            </div>
          </div>
          <div class="source-desc">{{ s.description }}</div>
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
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import http from '../api/http'

const ICONS: Record<string, string> = {
  jira: '🧩',
  dingtalk: '💬',
  gerrit: '🔀',
  gitlab: '🦊',
  confluence: '📚',
}

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
  padding: 28px 40px 60px;
  max-width: 1040px;
  margin: 0 auto;
  height: 100%;
  overflow-y: auto;
}
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 18px; }
.page-head h2 { font-size: 24px; font-weight: 700; color: var(--text); margin: 0 0 4px; }
.muted { color: var(--text-3); font-size: 13px; }
.empty { color: var(--text-3); padding: 40px; text-align: center; }
.source-list { display: flex; flex-direction: column; gap: 14px; }
.source-card {
  display: flex;
  gap: 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 18px;
  box-shadow: var(--shadow-sm);
  transition: box-shadow 0.15s, border-color 0.15s;
}
.source-card:hover { border-color: var(--border-strong); box-shadow: var(--shadow); }
.source-icon {
  width: 40px;
  height: 40px;
  flex: 0 0 auto;
  border-radius: 10px;
  background: var(--surface-2);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
}
.source-main { flex: 1; min-width: 0; }
.source-card-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.source-title { display: flex; align-items: center; gap: 8px; }
.source-name { font-size: 15px; font-weight: 600; color: var(--text); }
.source-desc { font-size: 12px; color: var(--text-3); margin-top: 4px; }
.source-config {
  font-size: 12px;
  color: var(--text-2);
  margin-top: 8px;
  background: var(--surface-2);
  border-radius: 6px;
  padding: 5px 10px;
  display: inline-block;
}
.source-status {
  font-size: 12px;
  color: var(--text-2);
  background: var(--primary-weak);
  border-radius: 8px;
  padding: 8px 12px;
  margin-top: 10px;
}
.source-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
</style>
