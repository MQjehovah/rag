<template>
  <div class="flags-page">
    <div v-loading="loading" class="flags-list">
      <div v-for="(value, name) in flags" :key="name" class="flag-item">
        <div class="flag-info">
          <div class="flag-name">{{ name }}</div>
          <div class="flag-desc">{{ FLAG_DESC[name] || '' }}</div>
        </div>
        <el-switch
          :model-value="value"
          :disabled="WRITE_LOCKED.has(name)"
          @change="(v: boolean) => toggle(name, v)"
        />
      </div>
      <div v-if="!Object.keys(flags).length && !loading" class="empty-tip">无 Flag</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import http from '../../api/http'
import { loadFeatureFlags, setFeatureFlagValue } from '../../navigation/featureFlags'

const FLAG_DESC: Record<string, string> = {
  card_v3_enabled: 'Card V3 编译启用',
  unified_retrieval_enabled: '统一检索管线启用',
  debt_chat_enabled: '知识债务融入 Chat',
  card_graph_enabled: '知识图谱启用',
  wiki_topic_enabled: 'Wiki 主题启用',
  source_hub_enabled: '统一数据源入口与 Worker 抢任务总开关',
  dingtalk_connector_enabled: '允许统一入口执行钉钉 Connector',
  gitlab_connector_enabled: '允许统一入口执行 GitLab Connector',
  source_card_compile_enabled: '数据源内容进入 Card Proposal 编译',
}

const flags = ref<Record<string, boolean>>({})
const WRITE_LOCKED = new Set<string>([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    flags.value = await loadFeatureFlags(true)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载 Flag 失败')
  } finally {
    loading.value = false
  }
}

async function toggle(name: string, value: boolean) {
  try {
    const res = await http.post('/api/p7/flags', { flag: name, value })
    flags.value[name] = res.data.value
    setFeatureFlagValue(name, res.data.value)
    ElMessage.success(`${name} → ${value ? '开启' : '关闭'}`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '切换失败')
    await load() // 还原
  }
}

onMounted(load)
</script>

<style scoped>
.flags-page {
  height: 100%;
  overflow-y: auto;
}
.flags-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.flag-item {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 14px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.flag-name {
  font-size: 14px;
  font-weight: 600;
  color: #111827;
  font-family: monospace;
}
.flag-desc {
  font-size: 12px;
  color: #6b7280;
  margin-top: 2px;
}
.empty-tip {
  font-size: 13px;
  color: #9ca3af;
  padding: 24px;
  text-align: center;
}
</style>
