<template>
  <div class="chat-page">
    <div class="chat-toolbar">
      <el-button size="small" @click="importDialogVisible = true">导入知识</el-button>
    </div>
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
        :rows="2"
        placeholder="输入问题，按 Enter 发送..."
        resize="none"
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

    <el-dialog v-model="importDialogVisible" title="导入知识" width="600px">
      <el-tabs v-model="importTab">
        <el-tab-pane label="文件上传" name="file">
          <el-upload
            :auto-upload="false"
            :limit="1"
            :on-change="handleFileChange"
            accept=".pdf,.txt,.md,.doc,.docx,.csv,.json"
          >
            <el-button>选择文件</el-button>
            <template #tip><div class="upload-tip">支持 PDF、Word、TXT、Markdown 等格式</div></template>
          </el-upload>
        </el-tab-pane>
        <el-tab-pane label="URL" name="url">
          <el-input v-model="importUrl" placeholder="输入网页 URL" />
        </el-tab-pane>
        <el-tab-pane label="文本" name="text">
          <el-input v-model="importText" type="textarea" :rows="6" placeholder="粘贴文本内容" />
        </el-tab-pane>
      </el-tabs>
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importLoading" @click="handleImport">分析并导入</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="confirmDialogVisible" title="确认导入" width="600px">
      <div v-if="!confirmForm.should_save" class="save-summary">LLM 判断该内容不值得保存（广告/无效内容等）</div>
      <template v-else>
        <el-form label-width="100px">
          <el-form-item label="操作">
            <el-tag v-if="confirmForm.action === 'create_notebook'" type="success">新建笔记本 + 笔记</el-tag>
            <el-tag v-else-if="confirmForm.action === 'update_note'" type="warning">更新已有笔记</el-tag>
            <el-tag v-else>新建笔记</el-tag>
          </el-form-item>
          <el-form-item label="标题">
            <el-input v-model="confirmForm.title" />
          </el-form-item>
          <el-form-item v-if="confirmForm.action === 'create_notebook'" label="新建笔记本">
            <el-input v-model="confirmForm.new_notebook_name" placeholder="新笔记本名称" />
          </el-form-item>
          <el-form-item v-else-if="confirmForm.action === 'update_note'" label="更新笔记">
            <el-select v-model="confirmForm.update_page_id" placeholder="选择要更新的笔记" style="width: 100%">
              <el-option v-for="p in confirmForm.pages" :key="p.id" :label="p.title" :value="p.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-else label="笔记本">
            <el-select v-model="confirmForm.notebook_id" clearable placeholder="选择笔记本" style="width: 100%">
              <el-option v-for="nb in confirmForm.notebooks" :key="nb.id" :label="nb.name" :value="nb.id" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="confirmForm.summary" label="摘要">
            <div class="save-summary">{{ confirmForm.summary }}</div>
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="confirmDialogVisible = false">取消</el-button>
        <el-button v-if="confirmForm.should_save" type="primary" :loading="confirmLoading" @click="confirmImport">确认保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import type { UploadFile as ElUploadFile } from 'element-plus'
import http from '../api/http'

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
const saveLoading = ref(false)
const importLoading = ref(false)
const confirmLoading = ref(false)

const saveDialogVisible = ref(false)
const saveForm = ref<{ should_save: boolean; action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; summary: string; content: string; notebooks: { id: string; name: string }[]; pages: { id: string; title: string }[]; msgIdx: number }>({
  should_save: true, action: 'create_note', title: '', notebook_id: null, new_notebook_name: '', update_page_id: null, summary: '', content: '', notebooks: [], pages: [], msgIdx: -1,
})

const importDialogVisible = ref(false)
const importTab = ref('file')
const importUrl = ref('')
const importText = ref('')
const selectedFile = ref<File | null>(null)

const confirmDialogVisible = ref(false)
const confirmForm = ref<{ should_save: boolean; action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; summary: string; content: string; notebooks: { id: string; name: string }[]; pages: { id: string; title: string }[] }>({
  should_save: true, action: 'create_note', title: '', notebook_id: null, new_notebook_name: '', update_page_id: null, summary: '', content: '', notebooks: [], pages: [],
})

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

const _executeSave = async (form: { action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; content: string }) => {
  const action = form.action || 'create_note'

  if (action === 'create_notebook' && form.new_notebook_name) {
    const nbRes = await http.post('/api/notebooks', { name: form.new_notebook_name })
    await http.post('/api/pages', {
      title: form.title,
      content: form.content,
      notebook_id: nbRes.data.id,
    })
  } else if (action === 'update_note' && form.update_page_id) {
    await http.put(`/api/pages/${form.update_page_id}`, {
      content: form.content,
    })
  } else {
    await http.post('/api/pages', {
      title: form.title,
      content: form.content,
      notebook_id: form.notebook_id || undefined,
    })
  }
}

const confirmSaveNote = async () => {
  saveLoading.value = true
  try {
    await _executeSave(saveForm.value)
    ElMessage.success('笔记已保存')
    saveDialogVisible.value = false
  } catch (e: any) {
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    saveLoading.value = false
  }
}

const handleFileChange = (file: ElUploadFile) => {
  selectedFile.value = file.raw || null
}

const handleImport = async () => {
  if (importTab.value === 'file' && !selectedFile.value) {
    ElMessage.warning('请选择文件')
    return
  }
  if (importTab.value === 'url' && !importUrl.value.trim()) {
    ElMessage.warning('请输入URL')
    return
  }
  if (importTab.value === 'text' && !importText.value.trim()) {
    ElMessage.warning('请输入文本')
    return
  }

  importLoading.value = true
  try {
    let res
    if (importTab.value === 'file') {
      const formData = new FormData()
      formData.append('file', selectedFile.value!)
      res = await http.post('/api/chat/import/file', formData)
    } else if (importTab.value === 'url') {
      res = await http.post('/api/chat/import/url', { url: importUrl.value })
    } else {
      res = await http.post('/api/chat/import/text', { text: importText.value })
    }
    confirmForm.value = res.data
    confirmDialogVisible.value = true
  } catch (e: any) {
    ElMessage.error('导入失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    importLoading.value = false
  }
}

const confirmImport = async () => {
  confirmLoading.value = true
  try {
    await _executeSave(confirmForm.value)
    ElMessage.success('知识已导入')
    confirmDialogVisible.value = false
    importDialogVisible.value = false
    selectedFile.value = null
    importUrl.value = ''
    importText.value = ''
  } catch (e: any) {
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    confirmLoading.value = false
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
.chat-toolbar {
  max-width: 900px;
  width: 100%;
  margin: 0 auto;
  padding: 8px 20px 0;
  display: flex;
  justify-content: flex-end;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px 20px;
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
.message-actions {
  margin-top: 6px;
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
.save-summary {
  font-size: 13px;
  color: #606266;
  background: #f5f7fa;
  padding: 8px 12px;
  border-radius: 6px;
  line-height: 1.5;
}
.upload-tip {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}
</style>
