<template>
  <div class="chat-page">
    <div class="chat-messages" ref="messagesRef">
      <div v-if="messages.length === 0" class="chat-empty">
        <div class="empty-icon">💬</div>
        <p>向 AI 助手提问，它将基于知识库内容回答</p>
      </div>
      <div v-for="(msg, idx) in messages" :key="idx" :class="['chat-message', msg.role]">
        <div class="message-avatar">{{ msg.role === 'user' ? 'U' : 'AI' }}</div>
        <div class="message-body">
          <div class="message-content markdown-body" :class="{ typing: !msg.content && loading }" v-html="msg.content ? renderContent(msg.content) : '思考中...'"></div>
        <div v-if="msg.sources && msg.sources.length" class="message-sources">
          <span class="sources-label">引用</span>
          <el-tooltip
            v-for="(src, i) in msg.sources"
            :key="src.id"
            placement="top"
            :show-after="200"
            :hide-after="100"
            popper-class="source-tooltip"
          >
            <template #content>
              <div class="source-tooltip-title">
                <span class="source-tooltip-index">{{ i + 1 }}</span>
                {{ src.title }}
              </div>
              <div v-if="src.chunks && src.chunks.length" class="source-tooltip-chunk">
                <span v-if="src.chunks[0].context" class="source-tooltip-ctx">{{ src.chunks[0].context }}</span>
                <div class="source-tooltip-text">{{ src.chunks[0].content }}</div>
              </div>
              <div v-if="src.images && src.images.length" class="source-tooltip-imgs">
                <img
                  v-for="img in src.images.slice(0, 3)"
                  :key="img"
                  :src="resolveUrl(img)"
                  loading="lazy"
                  @error="hideImg"
                />
              </div>
            </template>
            <router-link
              :to="{ path: '/notes', query: { page: src.id } }"
              class="source-chip"
              @click.prevent="openSource(src)"
            >
              [{{ i + 1 }}] {{ src.title }}
            </router-link>
          </el-tooltip>
        </div>
        <div v-if="msg.role === 'assistant' && sourceImages(msg).length" class="message-images">
          <a
            v-for="img in sourceImages(msg)"
            :key="img"
            :href="resolveUrl(img)"
            target="_blank"
            rel="noopener"
            class="message-image"
          >
            <img :src="resolveUrl(img)" loading="lazy" @error="hideImg" />
          </a>
        </div>
          <div v-if="msg.role === 'assistant' && msg.content && !loading" class="message-actions">
            <el-button size="small" text type="primary" @click="handleSaveNote(idx)">保存为笔记</el-button>
          </div>
        </div>
      </div>
    </div>
    <div class="chat-input-area">
      <el-input
        v-model="input"
        type="textarea"
        :rows="1"
        placeholder="输入问题，按 Enter 发送..."
        resize="none"
        autosize
        @keydown.enter.exact.prevent="sendMessage"
        :disabled="loading"
      />
      <el-button type="primary" :loading="loading" @click="sendMessage" :disabled="!input.trim()">发送</el-button>
    </div>

    <el-dialog v-model="saveDialogVisible" title="保存为笔记" width="600px">
      <div v-if="!saveForm.should_save" class="save-summary">LLM 判断该内容不值得保存为知识笔记</div>
      <template v-else>
        <el-form label-width="100px">
          <el-form-item label="操作">
            <el-tag v-if="saveForm.action === 'create_notebook'" type="success">新建笔记本 + 笔记</el-tag>
            <el-tag v-else-if="saveForm.action === 'update_note'" type="warning">更新已有笔记</el-tag>
            <el-tag v-else>新建笔记</el-tag>
          </el-form-item>
          <el-form-item label="标题">
            <el-input v-model="saveForm.title" />
          </el-form-item>
          <el-form-item v-if="saveForm.action === 'create_notebook'" label="新建笔记本">
            <el-input v-model="saveForm.new_notebook_name" placeholder="新笔记本名称" />
          </el-form-item>
          <el-form-item v-else-if="saveForm.action === 'update_note'" label="更新笔记">
            <el-select v-model="saveForm.update_page_id" placeholder="选择要更新的笔记" style="width: 100%">
              <el-option v-for="p in saveForm.pages" :key="p.id" :label="p.title" :value="p.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-else label="笔记本">
            <el-select v-model="saveForm.notebook_id" clearable placeholder="选择笔记本" style="width: 100%">
              <el-option v-for="nb in saveForm.notebooks" :key="nb.id" :label="nb.name" :value="nb.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="saveForm.summary" label="摘要">
            <div class="save-summary">{{ saveForm.summary }}</div>
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="saveDialogVisible = false">取消</el-button>
        <el-button v-if="saveForm.should_save" type="primary" :loading="saveLoading" @click="confirmSaveNote">确认保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import http from '../api/http'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

const md = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
  highlight(str: string, lang: string) {
    if (lang && hljs.getLanguage(lang)) {
      try { return (hljs.highlight(str, { language: lang }) as any).value } catch {}
    }
    return (hljs.highlightAuto(str) as any).value
  },
})

const router = useRouter()

interface Source {
  id: string
  title: string
  chunks?: { content: string; context?: string }[]
  images?: string[]
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  sources?: Source[]
}

const STORAGE_KEY = 'rag_chat_history_v1'

const loadHistory = (): Message[] => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // Drop malformed messages and unfinished assistant answers (empty
    // content means the page was refreshed mid-stream).
    return parsed.filter((m: any) =>
      m &&
      (m.role === 'user' || m.role === 'assistant') &&
      typeof m.content === 'string' &&
      (m.role === 'user' || m.content.length > 0)
    )
  } catch {
    return []
  }
}

const messages = ref<Message[]>(loadHistory())
const input = ref('')
const loading = ref(false)
const messagesRef = ref<HTMLElement>()
const saveLoading = ref(false)

const saveDialogVisible = ref(false)
const saveForm = ref<{ should_save: boolean; action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; summary: string; content: string; notebooks: { id: string; name: string }[]; pages: { id: string; title: string }[]; msgIdx: number }>({
  should_save: true, action: 'create_note', title: '', notebook_id: null, new_notebook_name: '', update_page_id: null, summary: '', content: '', notebooks: [], pages: [], msgIdx: -1,
})

const scrollToBottom = () => {
  nextTick(() => {
    if (messagesRef.value) {
      messagesRef.value.scrollTop = messagesRef.value.scrollHeight
    }
  })
}

let saveTimer: number | null = null
watch(messages, () => {
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = window.setTimeout(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.value))
    } catch { /* storage full / unavailable */ }
  }, 600)
}, { deep: true })

onMounted(() => {
  scrollToBottom()
})

onBeforeUnmount(() => {
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.value))
  } catch { /* ignore */ }
})

const renderContent = (text: string) => {
  return md.render(text)
}

const sendMessage = async () => {
  const query = input.value.trim()
  if (!query || loading.value) return

  const history = messages.value
    .filter(m => m.content)
    .slice(-8)
    .map(m => ({ role: m.role, content: m.content }))

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
      body: JSON.stringify({ query, history }),
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

const openSource = (src: any) => {
  const chunk = src.chunks && src.chunks[0]
  try {
    sessionStorage.setItem('cite-snippet', JSON.stringify({
      pageId: src.id,
      index: chunk ? chunk.chunk_index ?? 0 : 0,
      text: chunk ? String(chunk.content || '').slice(0, 120) : '',
    }))
  } catch { /* ignore */ }
  router.push({
    path: '/notes',
    query: { page: src.id },
    hash: chunk ? `#c${chunk.chunk_index ?? 0}` : '',
  })
}

const resolveUrl = (u: string) => {
  if (/^(https?:|data:)/.test(u)) return u
  return window.location.origin + (u.startsWith('/') ? '' : '/') + u
}

const hideImg = (e: Event) => {
  const el = e.target as HTMLImageElement
  const src = el.getAttribute('src') || ''
  if (!src.includes('/api/upload/images/proxy') && /^https?:/.test(src)) {
    // First failure: retry once through the no-Referer backend proxy.
    el.src = window.location.origin + '/api/upload/images/proxy?url=' + encodeURIComponent(src)
    return
  }
  el.style.display = 'none'
}

const sourceImages = (msg: Message) => {
  const seen = new Set<string>()
  const out: string[] = []
  for (const s of msg.sources || []) {
    for (const img of s.images || []) {
      if (!seen.has(img)) {
        seen.add(img)
        out.push(img)
        if (out.length >= 9) return out
      }
    }
  }
  return out
}

const handleSaveNote = async (idx: number) => {
  let queryIdx = -1
  for (let i = idx - 1; i >= 0; i--) {
    if (messages.value[i].role === 'user') {
      queryIdx = i
      break
    }
  }
  if (queryIdx < 0) return

  const query = messages.value[queryIdx].content
  const answer = messages.value[idx].content

  saveLoading.value = true
  try {
    const res = await http.post('/api/chat/save-note', { query, answer })
    saveForm.value = { ...res.data, msgIdx: idx }
    saveDialogVisible.value = true
  } catch (e: any) {
    ElMessage.error('分析失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    saveLoading.value = false
  }
}

const confirmSaveNote = async () => {
  saveLoading.value = true
  try {
    const form = saveForm.value
    const action = form.action || 'create_note'
    if (action === 'create_notebook' && form.new_notebook_name) {
      const nbRes = await http.post('/api/notebooks', { name: form.new_notebook_name })
      await http.post('/api/pages', { title: form.title, content: form.content, notebook_id: nbRes.data.id })
    } else if (action === 'update_note' && form.update_page_id) {
      await http.put(`/api/pages/${form.update_page_id}`, { content: form.content })
    } else {
      await http.post('/api/pages', { title: form.title, content: form.content, notebook_id: form.notebook_id || undefined })
    }
    ElMessage.success('笔记已保存')
    saveDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    saveLoading.value = false
  }
}
</script>

<style scoped>
@import 'highlight.js/styles/github-dark.css';

.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #0f172a;
}
.chat-toolbar {
  max-width: 900px;
  width: 100%;
  margin: 0 auto;
  padding: 12px 24px 0;
  display: flex;
  justify-content: flex-end;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
  max-width: 900px;
  width: 100%;
  margin: 0 auto;
  box-sizing: border-box;
}
.chat-messages::-webkit-scrollbar { width: 6px; }
.chat-messages::-webkit-scrollbar-track { background: transparent; }
.chat-messages::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
.chat-empty {
  text-align: center;
  padding: 120px 20px;
  color: #475569;
}
.empty-icon {
  font-size: 56px;
  margin-bottom: 16px;
  opacity: 0.6;
}
.chat-message {
  display: flex;
  gap: 12px;
  margin-bottom: 24px;
  animation: fadeIn 0.3s ease;
}
@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
.chat-message.user {
  flex-direction: row-reverse;
}
.message-avatar {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
}
.chat-message.user .message-avatar {
  background: linear-gradient(135deg, #38bdf8, #6366f1);
}
.chat-message.assistant .message-avatar {
  background: #1e293b;
  border: 1px solid #334155;
}
.message-body {
  max-width: 75%;
  min-width: 0;
}
.message-content {
  padding: 14px 18px;
  border-radius: 14px;
  line-height: 1.7;
  font-size: 14px;
  word-break: break-word;
}
.chat-message.user .message-content {
  background: linear-gradient(135deg, #38bdf8, #6366f1);
  color: #fff;
  border-bottom-right-radius: 4px;
}
.chat-message.assistant .message-content {
  background: #1e293b;
  color: #e2e8f0;
  border: 1px solid #334155;
  border-bottom-left-radius: 4px;
}
.message-content.typing {
  color: #64748b;
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3) {
  color: #f1f5f9;
  margin: 16px 0 8px;
  font-weight: 600;
}
.markdown-body :deep(h1) { font-size: 20px; }
.markdown-body :deep(h2) { font-size: 17px; }
.markdown-body :deep(h3) { font-size: 15px; }
.markdown-body :deep(p) { margin: 6px 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 20px; margin: 6px 0; }
.markdown-body :deep(li) { margin: 3px 0; }
.markdown-body :deep(code) {
  background: rgba(0,0,0,0.3);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
}
.markdown-body :deep(pre) {
  background: #0c1222;
  border-radius: 8px;
  padding: 14px;
  overflow-x: auto;
  margin: 10px 0;
  border: 1px solid #1e293b;
}
.markdown-body :deep(pre code) {
  background: none;
  padding: 0;
  font-size: 13px;
  line-height: 1.6;
}
.markdown-body :deep(blockquote) {
  border-left: 3px solid #38bdf8;
  padding-left: 12px;
  margin: 8px 0;
  color: #94a3b8;
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 10px 0;
}
.markdown-body :deep(th), .markdown-body :deep(td) {
  border: 1px solid #334155;
  padding: 6px 12px;
  text-align: left;
}
.markdown-body :deep(th) {
  background: #1e293b;
  font-weight: 600;
}
.markdown-body :deep(a) {
  color: #38bdf8;
  text-decoration: none;
}
.markdown-body :deep(a:hover) {
  text-decoration: underline;
}
.markdown-body :deep(strong) { color: #f1f5f9; }
.message-sources {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
}
.sources-label {
  font-size: 11px;
  color: #64748b;
  margin-bottom: 2px;
}
.source-chip {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  font-size: 12px;
  color: #94a3b8;
  text-decoration: none;
  background: rgba(148, 163, 184, 0.08);
  border: 1px solid rgba(148, 163, 184, 0.14);
  padding: 1px 9px;
  border-radius: 10px;
  line-height: 18px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: all 0.15s;
}
.source-chip:hover {
  color: #7dd3fc;
  border-color: rgba(56, 189, 248, 0.35);
  background: rgba(56, 189, 248, 0.1);
}
.message-images {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.message-image {
  display: block;
  width: 72px;
  height: 72px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #334155;
  background: #1e293b;
}
.message-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.markdown-body :deep(img) {
  max-width: 100%;
  border-radius: 8px;
}
:global(.source-tooltip-imgs) {
  display: flex;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}
:global(.source-tooltip-imgs img) {
  width: 64px;
  height: 64px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid #334155;
}
:global(.source-tooltip) {
  background: #1e293b !important;
  border: 1px solid #334155 !important;
  color: #e2e8f0 !important;
  max-width: 420px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
}
:global(.source-tooltip .el-popper__arrow::before) {
  background: #1e293b !important;
  border-color: #334155 !important;
}
:global(.source-tooltip-title) {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #f1f5f9;
  margin-bottom: 6px;
}
:global(.source-tooltip-index) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border-radius: 5px;
  background: rgba(56, 189, 248, 0.18);
  color: #7dd3fc;
  font-size: 11px;
}
:global(.source-tooltip-ctx) {
  display: block;
  color: #7dd3fc;
  font-size: 11px;
  margin-bottom: 4px;
}
:global(.source-tooltip-text) {
  font-size: 12.5px;
  line-height: 1.6;
  color: #cbd5e1;
  word-break: break-word;
  max-height: 220px;
  overflow-y: auto;
}
.message-actions {
  margin-top: 8px;
}
.chat-input-area {
  max-width: 900px;
  width: 100%;
  margin: 0 auto;
  padding: 12px 24px 16px;
  display: flex;
  gap: 10px;
  align-items: center;
  box-sizing: border-box;
}
.chat-input-area :deep(.el-textarea) {
  flex: 1;
}
.chat-input-area :deep(.el-textarea__inner) {
  background: #1e293b;
  border: 1px solid #334155;
  color: #e2e8f0;
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 14px;
}
.chat-input-area :deep(.el-textarea__inner:focus) {
  border-color: #38bdf8;
}
.chat-input-area :deep(.el-textarea__inner::placeholder) {
  color: #475569;
}
.chat-input-area > .el-button {
  border-radius: 10px;
  height: 36px;
  flex-shrink: 0;
}
</style>
