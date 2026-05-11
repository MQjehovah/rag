<template>
  <div class="chat-page">
    <div class="chat-messages" ref="messagesRef">
      <div v-if="messages.length === 0" class="chat-empty">
        <div class="empty-icon">💬</div>
        <p>向 AI 助手提问，它将基于知识库内容回答</p>
      </div>
      <div v-for="(msg, idx) in messages" :key="idx" :class="['chat-message', msg.role]">
        <div class="message-avatar">{{ msg.role === 'user' ? '👤' : '🤖' }}</div>
        <div class="message-body">
          <div class="message-content" :class="{ typing: !msg.content && loading }" v-html="msg.content ? renderContent(msg.content) : '思考中...'"></div>
          <div v-if="msg.sources && msg.sources.length" class="message-sources">
            <div class="sources-label">引用来源：</div>
            <router-link
              v-for="src in msg.sources"
              :key="src.id"
              :to="{ path: '/', query: { page: src.id } }"
              class="source-link"
            >{{ src.title }}</router-link>
          </div>
        </div>
      </div>
    </div>
    <div class="chat-input-area">
      <el-input
        v-model="input"
        type="textarea"
        :rows="2"
        placeholder="输入问题，按 Enter 发送..."
        resize="none"
        @keydown.enter.exact.prevent="sendMessage"
        :disabled="loading"
      />
      <el-button type="primary" :loading="loading" @click="sendMessage" :disabled="!input.trim()">发送</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'

interface Source {
  id: string
  title: string
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: Source[]
}

const messages = ref<Message[]>([])
const input = ref('')
const loading = ref(false)
const messagesRef = ref<HTMLElement>()

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesRef.value) {
      messagesRef.value.scrollTop = messagesRef.value.scrollHeight
    }
  })
}

const renderContent = (text: string) => {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\n/g, '<br>')
}

const sendMessage = async () => {
  const query = input.value.trim()
  if (!query || loading.value) return

  messages.value.push({ role: 'user', content: query })
  input.value = ''
  loading.value = true
  scrollToBottom()

  const assistantMsg: Message = { role: 'assistant', content: '' }
  messages.value.push(assistantMsg)

  try {
    const token = localStorage.getItem('token')
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({ query }),
    })

    if (!resp.ok) {
      const err = await resp.json()
      assistantMsg.content = `错误: ${err.detail || resp.statusText}`
      loading.value = false
      scrollToBottom()
      return
    }

    const reader = resp.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const dataStr = line.slice(6)
        if (dataStr.trim() === '[DONE]') continue

        try {
          const data = JSON.parse(dataStr)
          if (data.type === 'content') {
            assistantMsg.content += data.content
            scrollToBottom()
          } else if (data.type === 'sources') {
            assistantMsg.sources = data.sources
          } else if (data.type === 'error') {
            assistantMsg.content += `\n\n错误: ${data.content}`
          }
        } catch {
          // skip
        }
      }
    }
  } catch (e: any) {
    assistantMsg.content = `请求失败: ${e.message}`
  } finally {
    loading.value = false
    scrollToBottom()
  }
}
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #f5f7fa;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  max-width: 900px;
  width: 100%;
  margin: 0 auto;
  box-sizing: border-box;
}
.chat-empty {
  text-align: center;
  padding: 80px 20px;
  color: #909399;
}
.empty-icon {
  font-size: 48px;
  margin-bottom: 16px;
}
.chat-message {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
}
.chat-message.user {
  flex-direction: row-reverse;
}
.message-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex-shrink: 0;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.message-body {
  max-width: 70%;
  min-width: 0;
}
.chat-message.user .message-body {
  align-items: flex-end;
}
.message-content {
  background: #fff;
  padding: 12px 16px;
  border-radius: 12px;
  line-height: 1.6;
  font-size: 14px;
  color: #303133;
  word-break: break-word;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.chat-message.user .message-content {
  background: #409eff;
  color: #fff;
}
.message-content.typing {
  color: #909399;
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.message-sources {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.sources-label {
  font-size: 12px;
  color: #909399;
}
.source-link {
  font-size: 12px;
  color: #409eff;
  text-decoration: none;
  background: #ecf5ff;
  padding: 2px 8px;
  border-radius: 4px;
}
.source-link:hover {
  background: #d9ecff;
}
.chat-input-area {
  max-width: 900px;
  width: 100%;
  margin: 0 auto;
  padding: 16px 20px;
  display: flex;
  gap: 12px;
  align-items: flex-end;
  box-sizing: border-box;
}
.chat-input-area .el-textarea {
  flex: 1;
}
</style>
