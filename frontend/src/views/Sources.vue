<template>
  <div class="page-shell">
    <header class="page-header">
      <div class="ph-main">
        <div class="ph-eyebrow">数据与管道</div>
        <h1 class="page-title">数据源</h1>
        <p class="page-desc">企业系统作为插件接入：拉取原始内容 → 形成笔记 → 编译进知识库。</p>
      </div>
      <div class="ph-actions">
        <div class="stat-row">
          <div class="stat-item"><span class="stat-num">{{ sources.length }}</span><span class="stat-label">已接入</span></div>
          <div class="stat-item"><span class="stat-num">{{ enabledCount }}</span><span class="stat-label">已启用</span></div>
          <div class="stat-item"><span class="stat-num">{{ runningCount }}</span><span class="stat-label">同步中</span></div>
        </div>
        <el-button class="btn-refresh" :loading="loading" @click="load">
          <el-icon><RefreshCw /></el-icon><span>刷新</span>
        </el-button>
      </div>
    </header>

    <div class="page-body">
    <div v-if="loading && !sources.length" class="hint">正在加载数据源…</div>
    <div v-else-if="!sources.length" class="hint">暂无已接入的数据源</div>

    <div v-else class="grid">
      <article v-for="s in sources" :key="s.key" class="src">
        <div class="src-top">
          <div class="tile" :style="tileStyle(s.key)">
            <el-icon :size="18"><component :is="asset(s.key).icon" /></el-icon>
          </div>
          <div class="src-id">
            <div class="src-name">{{ s.name }}</div>
            <div class="src-key">{{ s.key }}</div>
          </div>
          <span class="pill" :class="s.enabled ? 'on' : 'off'">
            <i class="status-dot" :class="s.enabled ? 'ok' : 'off'" />{{ s.enabled ? '已启用' : '未启用' }}
          </span>
        </div>

        <p class="src-desc">{{ s.description }}</p>

        <div class="meta">
          <span class="meta-k">配置</span>
          <span class="meta-v" :title="s.config">{{ s.config || '—' }}</span>
        </div>

        <div v-if="s.status && s.status.message" class="src-status" :class="{ run: s.status.running }">
          <div class="status-line">
            <i class="status-dot" :class="s.status.running ? 'run' : (s.enabled ? 'ok' : 'off')" />
            <span class="status-msg">{{ s.status.message }}</span>
          </div>
          <template v-if="s.status.running && s.status.total > 0">
            <el-progress
              :percentage="Math.round(s.status.processed / s.status.total * 100)"
              :stroke-width="5"
              :show-text="false"
              style="margin-top: 10px"
            />
            <div class="prog-txt">{{ s.status.processed }} / {{ s.status.total }}</div>
          </template>
        </div>

        <div class="src-foot">
          <el-button link class="btn-test" :loading="testing === s.key" @click="testSource(s)">测试连接</el-button>
          <div class="foot-right">
            <el-select v-if="s.key === 'jira'" v-model="syncScope" size="small" style="width: 158px">
              <el-option v-for="o in scopeOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-button v-if="s.status?.running" size="small" @click="cancelSync(s)">取消</el-button>
            <el-button
              size="small"
              type="primary"
              :disabled="!s.enabled || s.status?.running"
              :loading="s.status?.running"
              @click="syncSource(s)"
            >{{ s.status?.running ? '同步中' : '立即同步' }}</el-button>
          </div>
        </div>
      </article>
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import {
  RefreshCw, Plug, Layers, MessageSquare, GitMerge, Gitlab, BookOpen,
} from 'lucide-vue-next'
import http from '../api/http'

const ASSETS: Record<string, { icon: any; color: string }> = {
  jira: { icon: Layers, color: '#2684ff' },
  dingtalk: { icon: MessageSquare, color: '#3296fa' },
  gerrit: { icon: GitMerge, color: '#0ea5e9' },
  gitlab: { icon: Gitlab, color: '#fc6d26' },
  confluence: { icon: BookOpen, color: '#1868db' },
}
const asset = (key: string) => ASSETS[key] || { icon: Plug, color: '#64748b' }
const tileStyle = (key: string) => {
  const c = asset(key).color
  return { color: c, background: c + '1a', borderColor: c + '33' }
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

const enabledCount = computed(() => sources.value.filter(s => s.enabled).length)
const runningCount = computed(() => sources.value.filter(s => s.status?.running).length)

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
.btn-refresh { display: inline-flex; align-items: center; gap: 6px; }
.hint { color: var(--text-3); padding: 48px; text-align: center; font-size: 13px; }

/* ---- 卡片栅格 ---- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 18px;
}
.src {
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  box-shadow: var(--shadow-sm);
  transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s;
}
.src:hover { border-color: var(--border-strong); box-shadow: var(--shadow); transform: translateY(-1px); }

.src-top { display: flex; align-items: flex-start; gap: 12px; }
.tile {
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
}
.src-id { flex: 1; min-width: 0; }
.src-name { font-size: 14.5px; font-weight: 600; color: var(--text); line-height: 1.3; }
.src-key { font-size: 11.5px; color: var(--text-3); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; margin-top: 1px; }

.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 500;
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}
.pill.on { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
.pill.off { color: var(--text-3); background: var(--surface-2); }

.src-desc { font-size: 13px; color: var(--text-2); line-height: 1.6; margin: 14px 0 0; }

.meta {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-top: 12px;
  padding: 8px 11px;
  background: var(--surface-2);
  border-radius: var(--radius);
  font-size: 12px;
}
.meta-k { color: var(--text-3); flex: 0 0 auto; }
.meta-v {
  color: var(--text-2);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.src-status {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: var(--radius);
  background: var(--surface-2);
  border: 1px solid var(--border);
}
.src-status.run { background: var(--primary-weak); border-color: transparent; }
.status-line { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text-2); }
.status-msg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prog-txt { font-size: 11.5px; color: var(--text-3); margin-top: 4px; text-align: right; }

.src-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: auto;
  padding-top: 14px;
  margin-top: 16px;
  border-top: 1px solid var(--border);
}
.btn-test { font-size: 13px; color: var(--text-2); }
.foot-right { display: flex; align-items: center; gap: 8px; }
</style>
