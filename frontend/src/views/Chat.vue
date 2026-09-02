<template>
  <div class="chat-page">
    <div class="chat-messages">
      <div v-if="!lastResult && !requestError" class="chat-empty">
        <p>AI 问答：Wiki 优先，LLM 不可用时自动切换为资料检索模式。</p>
      </div>

      <el-alert
        v-if="requestError"
        type="error"
        :closable="false"
        title="请求失败，请稍后重试。"
        :description="requestError"
        class="chat-alert"
      />

      <template v-if="lastResult">
        <el-alert
          v-if="isRetrievalUnavailable"
          type="error"
          :closable="false"
          title="检索服务不可用，暂时无法获取资料。"
          class="chat-alert"
        />
        <el-alert
          v-else-if="lastResult.response_mode === 'retrieval_only'"
          type="warning"
          :closable="false"
          title="当前 AI 回答服务不可用，已切换为资料检索模式。"
          class="chat-alert"
        />
        <el-alert
          v-else-if="lastResult.response_mode === 'insufficient' && !lastResult.knowledge_missing"
          type="info"
          :closable="false"
          title="已找到部分资料，但不足以生成完整回答。"
          class="chat-alert"
        />
        <el-alert
          v-else-if="lastResult.knowledge_missing"
          type="info"
          :closable="false"
          title="知识库中暂无相关内容。"
          class="chat-alert"
        />

        <div v-if="lastResult.answer" class="chat-answer">{{ lastResult.answer }}</div>

        <!-- J-4 回答反馈 -->
        <div v-if="showFeedback" class="chat-feedback">
          <template v-if="feedbackState !== 'thanks'">
            <el-button size="small" @click="sendHelpful(true)">👍 有帮助</el-button>
            <el-button size="small" @click="showReasonPanel = true">👎 没解决问题</el-button>
          </template>
          <span v-else class="chat-feedback-thanks">感谢反馈</span>

          <div v-if="showReasonPanel" class="chat-feedback-reason">
            <el-radio-group v-model="reason">
              <el-radio value="incorrect">内容不对</el-radio>
              <el-radio value="incomplete">内容不全</el-radio>
            </el-radio-group>
            <el-input
              v-model="note"
              type="textarea"
              :rows="2"
              placeholder="补充说明（可选）"
              maxlength="2000"
            />
            <div class="chat-feedback-actions">
              <el-button size="small" type="primary" @click="sendHelpful(false)">保存</el-button>
              <el-button size="small" @click="showReasonPanel = false">取消</el-button>
            </div>
          </div>
        </div>

        <!-- J-4 没有获得充分授权答案 -->
        <div v-if="showAnswerNeeded" class="chat-feedback">
          <span v-if="neededSent" class="chat-feedback-thanks">已收到，我们会继续完善相关资料。</span>
          <el-button v-else size="small" @click="sendAnswerNeeded">我仍需要这个答案</el-button>
        </div>

        <div v-if="lastResult.wiki_results.length" class="chat-section">
          <div class="chat-section-title">Wiki 主题</div>
          <div v-for="w in lastResult.wiki_results" :key="w.wiki_page_id" class="chat-card">
            <div class="chat-card-title">
              <a :href="w.link">{{ w.title }}</a>
              <el-tag v-if="w.manually_edited" size="small" type="success">人工编辑</el-tag>
              <el-tag v-if="w.locked" size="small" type="info">已锁定</el-tag>
            </div>
            <div class="chat-card-content">{{ w.content }}</div>
            <div v-if="w.updated_at" class="chat-card-meta">更新时间：{{ w.updated_at }}</div>
          </div>
        </div>

        <div v-if="lastResult.raw_results.length" class="chat-section">
          <div class="chat-section-title">原始资料</div>
          <div v-for="r in lastResult.raw_results" :key="r.chunk_id" class="chat-card">
            <div class="chat-card-title">
              <a :href="r.link">{{ r.title }}</a>
              <el-tag v-if="r.source_type" size="small">{{ r.source_type }}</el-tag>
              <span class="chat-card-pos">#{{ r.chunk_index }}</span>
            </div>
            <div class="chat-card-content">{{ r.content }}</div>
            <div v-if="r.updated_at" class="chat-card-meta">更新时间：{{ r.updated_at }}</div>
          </div>
        </div>

        <div v-if="lastResult.degraded_reasons.length" class="chat-degraded">
          降级原因：{{ lastResult.degraded_reasons.join('、') }}
        </div>
      </template>
    </div>

    <div class="chat-input">
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

const feedbackState = ref<'idle' | 'thanks'>('idle')
const showReasonPanel = ref(false)
const reason = ref<'incorrect' | 'incomplete' | null>(null)
const note = ref('')
const neededSent = ref(false)

const isRetrievalUnavailable = computed(() =>
  lastResult.value?.degraded_reasons.includes('retrieval_unavailable') ?? false
)

// J-4：response_mode=answer 且后端已成功持久化 answer_id 时才显示 👍/👎
const showFeedback = computed(() => {
  const r = lastResult.value
  return !!r && r.response_mode === 'answer' && !!r.answer_id
})

// J-4：没有获得充分授权答案，且非服务降级、有 answer_id → 显示「我仍需要这个答案」
const showAnswerNeeded = computed(() => {
  const r = lastResult.value
  if (!r) return false
  return r.retrieval_completed && !r.service_degraded && !r.answer_eligible && !!r.answer_id
})

const resetFeedback = () => {
  feedbackState.value = 'idle'
  showReasonPanel.value = false
  reason.value = null
  note.value = ''
  neededSent.value = false
}

const send = async () => {
  const query = input.value.trim()
  if (!query || loading.value) return
  input.value = ''
  loading.value = true
  requestError.value = ''
  try {
    lastResult.value = await ragChatApi.ask(query)
    resetFeedback()
  } catch (err: any) {
    lastResult.value = null
    requestError.value = err?.response?.data?.detail || err?.message || '请求失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

const sendHelpful = async (helpful: boolean) => {
  if (!lastResult.value?.answer_id) return
  try {
    await ragChatApi.feedback({
      answer_id: lastResult.value.answer_id,
      helpful,
      reason: helpful ? null : (reason.value as 'incorrect' | 'incomplete' | null),
      note: helpful ? null : (note.value || null),
    })
    feedbackState.value = 'thanks'
    showReasonPanel.value = false
  } catch (err: any) {
    requestError.value = err?.response?.data?.detail || err?.message || '提交反馈失败，请稍后重试。'
  }
}

const sendAnswerNeeded = async () => {
  if (!lastResult.value?.answer_id) return
  try {
    await ragChatApi.answerNeeded(lastResult.value.answer_id)
    neededSent.value = true
  } catch (err: any) {
    requestError.value = err?.response?.data?.detail || err?.message || '提交失败，请稍后重试。'
  }
}
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  max-width: 900px;
  margin: 0 auto;
  padding: 16px;
  box-sizing: border-box;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  margin-bottom: 12px;
}
.chat-empty {
  color: #909399;
  text-align: center;
  padding: 40px 0;
}
.chat-alert {
  margin-bottom: 12px;
}
.chat-answer {
  background: #f5f7fa;
  border-radius: 8px;
  padding: 12px;
  white-space: pre-wrap;
  margin-bottom: 12px;
}
.chat-feedback {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.chat-feedback-thanks {
  color: #67c23a;
  font-size: 13px;
}
.chat-feedback-reason {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  margin-top: 4px;
}
.chat-feedback-actions {
  display: flex;
  gap: 8px;
}
.chat-section {
  margin-bottom: 12px;
}
.chat-section-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.chat-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 8px;
}
.chat-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.chat-card-pos {
  color: #909399;
  font-size: 12px;
}
.chat-card-content {
  white-space: pre-wrap;
  color: #303133;
}
.chat-card-meta {
  color: #909399;
  font-size: 12px;
  margin-top: 6px;
}
.chat-degraded {
  color: #e6a23c;
  font-size: 12px;
  margin-top: 8px;
}
.chat-input {
  display: flex;
  gap: 8px;
}
</style>
