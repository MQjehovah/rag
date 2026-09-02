<template>
  <div class="rag-chat-page">
    <div class="rag-chat-messages">
      <div v-if="!lastResult && !requestError" class="rag-chat-empty">
        <p>V4 降级问答：Wiki 优先，LLM 不可用时自动切换为资料检索模式。</p>
      </div>

      <el-alert
        v-if="requestError"
        type="error"
        :closable="false"
        title="请求失败，请稍后重试。"
        :description="requestError"
        class="rag-chat-alert"
      />

      <template v-if="lastResult">
        <el-alert
          v-if="isRetrievalUnavailable"
          type="error"
          :closable="false"
          title="检索服务不可用，暂时无法获取资料。"
          class="rag-chat-alert"
        />
        <el-alert
          v-else-if="lastResult.response_mode === 'retrieval_only'"
          type="warning"
          :closable="false"
          title="当前 AI 回答服务不可用，已切换为资料检索模式。"
          class="rag-chat-alert"
        />
        <el-alert
          v-else-if="lastResult.response_mode === 'insufficient' && !lastResult.knowledge_missing"
          type="info"
          :closable="false"
          title="已找到部分资料，但不足以生成完整回答。"
          class="rag-chat-alert"
        />
        <el-alert
          v-else-if="lastResult.knowledge_missing"
          type="info"
          :closable="false"
          title="知识库中暂无相关内容。"
          class="rag-chat-alert"
        />

        <div v-if="lastResult.answer" class="rag-chat-answer">{{ lastResult.answer }}</div>

        <div v-if="lastResult.wiki_results.length" class="rag-chat-section">
          <div class="rag-chat-section-title">Wiki 主题</div>
          <div v-for="w in lastResult.wiki_results" :key="w.wiki_page_id" class="rag-chat-card">
            <div class="rag-chat-card-title">
              <a :href="w.link">{{ w.title }}</a>
              <el-tag v-if="w.version_label" size="small" type="success">版本 {{ w.version_label }}</el-tag>
              <el-tag v-else-if="w.latest_version" size="small" type="success">最新 {{ w.latest_version }}</el-tag>
              <el-tag v-if="w.manually_edited" size="small" type="success">人工编辑</el-tag>
              <el-tag v-if="w.locked" size="small" type="info">已锁定</el-tag>
            </div>
            <div v-if="w.diff_notice" class="rag-chat-card-notice">⚠️ {{ w.diff_notice }}</div>
            <div class="rag-chat-card-content">{{ w.content }}</div>
            <div v-if="w.updated_at" class="rag-chat-card-meta">更新时间：{{ w.updated_at }}</div>
          </div>
        </div>

        <div v-if="lastResult.raw_results.length" class="rag-chat-section">
          <div class="rag-chat-section-title">原始资料</div>
          <div v-for="r in lastResult.raw_results" :key="r.chunk_id" class="rag-chat-card">
            <div class="rag-chat-card-title">
              <a :href="r.link">{{ r.title }}</a>
              <el-tag v-if="r.source_type" size="small">{{ r.source_type }}</el-tag>
              <span class="rag-chat-card-pos">#{{ r.chunk_index }}</span>
            </div>
            <div class="rag-chat-card-content">{{ r.content }}</div>
            <div v-if="r.updated_at" class="rag-chat-card-meta">更新时间：{{ r.updated_at }}</div>
          </div>
        </div>

        <div v-if="lastResult.degraded_reasons.length" class="rag-chat-degraded">
          降级原因：{{ lastResult.degraded_reasons.join('、') }}
        </div>
      </template>
    </div>

    <div class="rag-chat-input">
      <el-input
        v-model="input"
        type="textarea"
        :rows="1"
        placeholder="输入问题，按 Enter 发送..."
        resize="none"
        autosize
        @keydown.enter.exact.prevent="send"
        :disabled="loading"
      />
      <el-button type="primary" :loading="loading" @click="send" :disabled="!input.trim()">发送</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ragChatApi, type RagChatResponse } from '../api/ragChat'

const input = ref('')
const loading = ref(false)
const lastResult = ref<RagChatResponse | null>(null)
const requestError = ref('')

const isRetrievalUnavailable = computed(() =>
  lastResult.value?.degraded_reasons.includes('retrieval_unavailable') ?? false
)

const send = async () => {
  const query = input.value.trim()
  if (!query || loading.value) return
  input.value = ''
  loading.value = true
  requestError.value = ''
  try {
    lastResult.value = await ragChatApi.ask(query)
  } catch (err: any) {
    lastResult.value = null
    requestError.value = err?.response?.data?.detail || err?.message || '请求失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.rag-chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  max-width: 900px;
  margin: 0 auto;
  padding: 16px;
  box-sizing: border-box;
}
.rag-chat-messages {
  flex: 1;
  overflow-y: auto;
  margin-bottom: 12px;
}
.rag-chat-empty {
  color: #909399;
  text-align: center;
  padding: 40px 0;
}
.rag-chat-alert {
  margin-bottom: 12px;
}
.rag-chat-answer {
  background: #f5f7fa;
  border-radius: 8px;
  padding: 12px;
  white-space: pre-wrap;
  margin-bottom: 12px;
}
.rag-chat-section {
  margin-bottom: 12px;
}
.rag-chat-section-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.rag-chat-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 8px;
}
.rag-chat-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.rag-chat-card-pos {
  color: #909399;
  font-size: 12px;
}
.rag-chat-card-notice {
  color: #b45309;
  font-size: 12px;
  margin-bottom: 4px;
}
.rag-chat-card-content {
  white-space: pre-wrap;
  color: #303133;
}
.rag-chat-card-meta {
  color: #909399;
  font-size: 12px;
  margin-top: 6px;
}
.rag-chat-degraded {
  color: #e6a23c;
  font-size: 12px;
  margin-top: 8px;
}
.rag-chat-input {
  display: flex;
  gap: 8px;
}
</style>
