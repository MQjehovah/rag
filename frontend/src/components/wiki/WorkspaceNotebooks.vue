<template>
  <section class="ws-notebooks">
    <div class="wsn-head">
      <span class="wsn-title">绑定来源 Notebook</span>
      <el-tag v-if="!loading && !error && bindings.length" size="small" type="success">
        已绑定 {{ bindings.length }}
      </el-tag>
    </div>

    <div v-if="loading" v-loading="true" class="wsn-state">加载绑定中…</div>
    <div v-else-if="error" class="wsn-state wsn-error">
      <span>{{ error }}</span>
      <el-button size="small" @click="reload">重试</el-button>
    </div>
    <div v-else-if="!bindings.length" class="wsn-state">暂无已绑定的 Notebook</div>
    <ul v-else class="wsn-list">
      <li v-for="b in bindings" :key="b.binding_id" class="wsn-item">
        <span class="wsn-name">{{ b.notebook_name || b.notebook_id }}</span>
        <el-tag size="small" type="success">绑定中</el-tag>
        <span v-if="b.updated_at" class="wsn-time">更新 {{ fmtTime(b.updated_at) }}</span>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import {
  wikiWorkspacesApi,
  type NotebookBindingSummary,
} from '../../api/wikiWorkspaces'

/** Phase 8A：管理员工作区信息面板——只读展示 active Notebook 绑定。
 * 仅在管理员角色下由父组件渲染；不提供绑定/解绑操作。
 * 自管理加载状态与竞态：快速切换 workspace 时用序号丢弃迟到响应。
 */
const props = defineProps<{ workspaceId: string }>()

const bindings = ref<NotebookBindingSummary[]>([])
const loading = ref(false)
const error = ref('')
let seq = 0

async function reload() {
  if (!props.workspaceId) return
  const token = ++seq
  loading.value = true
  error.value = ''
  try {
    const data = await wikiWorkspacesApi.listNotebooks(props.workspaceId)
    if (token !== seq) return
    bindings.value = data.bindings || []
  } catch {
    if (token !== seq) return
    bindings.value = []
    error.value = '绑定信息加载失败'
  } finally {
    if (token === seq) loading.value = false
  }
}

watch(() => props.workspaceId, reload, { immediate: true })

function fmtTime(t: string): string {
  return (t || '').replace('T', ' ').slice(0, 16)
}
</script>

<style scoped>
.ws-notebooks {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 12px;
  background: #fafbfc;
}
.wsn-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.wsn-title {
  font-size: 12px;
  font-weight: 600;
  color: #374151;
}
.wsn-state {
  font-size: 12px;
  color: #9ca3af;
  min-height: 20px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.wsn-error {
  color: #b91c1c;
}
.wsn-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wsn-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.wsn-name {
  color: #111827;
  font-weight: 500;
}
.wsn-time {
  color: #9ca3af;
  flex: 1;
  text-align: right;
  white-space: nowrap;
}
</style>
