<template>
  <div class="search-container">
    <div class="search-box">
      <el-input
        v-model="question"
        placeholder="输入问题，检索 Wiki 主题或原始资料"
        size="large"
        clearable
        @keyup.enter="doSearch"
      />
      <el-button type="primary" size="large" :loading="loading" @click="doSearch">搜索</el-button>
    </div>

    <div v-if="loading" v-loading="true" class="loading-area"></div>

    <div v-else-if="result" class="result-area">
      <div v-if="result.wiki_results.length" class="result-section">
        <h3 class="result-title">Wiki 主题</h3>
        <div v-for="w in result.wiki_results" :key="w.wiki_page_id" class="result-card">
          <div class="result-card-title">
            <a :href="w.link">{{ w.title }}</a>
            <el-tag v-if="w.manually_edited" size="small" type="success">人工编辑</el-tag>
          </div>
          <div class="result-card-content">{{ w.content }}</div>
        </div>
      </div>

      <div v-if="result.raw_results.length" class="result-section">
        <h3 class="result-title">原始资料</h3>
        <div v-for="r in result.raw_results" :key="r.chunk_id" class="result-card">
          <div class="result-card-title">
            <a :href="r.link">{{ r.title }}</a>
            <el-tag v-if="r.source_type" size="small">{{ r.source_type }}</el-tag>
          </div>
          <div class="result-card-content">{{ r.content }}</div>
        </div>
      </div>

      <div v-if="result.degraded_reasons.length" class="degraded">
        降级原因：{{ result.degraded_reasons.join('、') }}
      </div>

      <div v-if="!result.wiki_results.length && !result.raw_results.length" class="empty-state">
        未找到相关 Wiki 主题或原始资料
      </div>
    </div>

    <div v-else class="empty-state">
      输入问题，检索 Wiki 主题或原始资料
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { searchV2, type SearchV2Result } from '../api/knowledge'

const route = useRoute()
const question = ref((route.query.q as string) || '')
const loading = ref(false)
const result = ref<SearchV2Result | null>(null)

async function doSearch() {
  const q = question.value.trim()
  if (!q) return
  loading.value = true
  result.value = null
  try {
    result.value = await searchV2(q)
  } catch {
    result.value = null
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (question.value) doSearch()
})
</script>

<style scoped>
.search-container {
  max-width: 900px;
  margin: 0 auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
  overflow-y: auto;
}
.search-box {
  display: flex;
  gap: 10px;
  align-items: center;
}
.loading-area {
  min-height: 120px;
}
.result-area {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.result-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.result-title {
  font-size: 14px;
  font-weight: 600;
  color: #374151;
  border-bottom: 1px solid #f3f4f6;
  padding-bottom: 8px;
}
.result-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
}
.result-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.result-card-content {
  white-space: pre-wrap;
  color: #303133;
}
.degraded {
  color: #e6a23c;
  font-size: 12px;
}
.empty-state {
  text-align: center;
  color: #9ca3af;
  padding: 60px 0;
}
</style>
