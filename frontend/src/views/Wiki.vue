<template>
  <div class="wiki-page">
    <aside class="wiki-sidebar">
      <div class="wiki-sidebar-header">
        <span class="wiki-brand">知识库 Wiki</span>
        <el-button
          v-if="isAdmin"
          size="small"
          type="primary"
          :loading="rebuilding"
          @click="rebuildWiki"
        >{{ running ? '编译中' : '重新编译' }}</el-button>
      </div>
      <el-input
        v-model="filterText"
        placeholder="搜索页面..."
        clearable
        size="small"
        class="wiki-filter"
      />
      <div class="wiki-cat-list">
        <div v-for="cat in filteredCategories" :key="cat.name" class="wiki-cat">
          <div class="wiki-cat-name">
            {{ cat.name }}
            <span class="wiki-cat-count">{{ cat.pages.length }}</span>
          </div>
          <div
            v-for="p in cat.pages"
            :key="p.id"
            class="wiki-page-item"
            :class="{ active: current && current.id === p.id }"
            @click="openPage(p.id)"
          >{{ p.title }}</div>
        </div>
        <el-empty
          v-if="!total && !running"
          description="Wiki 尚未生成，点右上角重新编译"
          :image-size="60"
        />
      </div>
    </aside>
    <main class="wiki-main">
      <div v-if="current" class="wiki-content-card">
        <div class="wiki-card-actions">
          <el-button v-if="!editing" size="small" text type="primary" @click="startEdit">编辑</el-button>
          <span v-else class="wiki-edit-tip">编辑中（下次编译会保留你的修改）</span>
        </div>
        <div class="wiki-crumb">{{ current.category }}</div>
        <h1 class="wiki-title">{{ current.title }}</h1>
        <div v-if="current.summary" class="wiki-summary">{{ current.summary }}</div>
        <template v-if="!editing">
          <div
            class="wiki-body markdown-body"
            v-html="renderContent(current.content)"
            @click="handleContentClick"
            @error.capture="handleImgError"
          ></div>
        </template>
        <template v-else>
          <div class="wiki-edit">
            <el-input v-model="editForm.category" placeholder="分类" size="small" class="wiki-edit-field" />
            <el-input v-model="editForm.summary" placeholder="摘要" size="small" class="wiki-edit-field" />
            <el-input
              v-model="editForm.content"
              type="textarea"
              :rows="20"
              placeholder="Markdown 正文，页面间引用用 [[页面标题]]"
              class="wiki-edit-content"
            />
            <div class="wiki-edit-actions">
              <el-button size="small" @click="editing = false">取消</el-button>
              <el-button size="small" type="primary" :loading="savingEdit" @click="saveEdit">保存</el-button>
            </div>
          </div>
        </template>
        <div v-if="current.sources && current.sources.length" class="wiki-sources">
          <span class="wiki-sources-label">来源笔记：</span>
          <router-link
            v-for="s in current.sources"
            :key="s.id"
            :to="{ path: '/notes', query: { page: s.id } }"
            class="wiki-source-link"
          >{{ s.title }}</router-link>
        </div>
      </div>
      <div v-else class="wiki-empty">
        <h2>{{ running ? 'Wiki 正在编译，请稍候...' : '选择左侧页面查看' }}</h2>
        <p v-if="running" class="wiki-progress">{{ progressText }}</p>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import http from '../api/http'
import { useAuthStore } from '../stores/auth'
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

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

interface WikiPageListItem {
  id: string
  title: string
  summary: string
}

const categories = ref<{ name: string; pages: WikiPageListItem[] }[]>([])
const total = ref(0)
const running = ref(false)
const rebuilding = ref(false)
const current = ref<any>(null)
const filterText = ref('')
const editing = ref(false)
const savingEdit = ref(false)
const editForm = ref({ content: '', summary: '', category: '' })

const isAdmin = computed(() =>
  (authStore.user?.groups || []).includes('__local_admin__')
)

const titleToId = computed(() => {
  const map: Record<string, string> = {}
  for (const cat of categories.value) {
    for (const p of cat.pages) map[p.title] = p.id
  }
  return map
})

const filteredCategories = computed(() => {
  if (!filterText.value) return categories.value
  const q = filterText.value.toLowerCase()
  return categories.value
    .map(c => ({ name: c.name, pages: c.pages.filter(p => p.title.toLowerCase().includes(q)) }))
    .filter(c => c.pages.length > 0)
})

const progressText = computed(() => {
  return status.value.total > 0 ? `${status.value.processed}/${status.value.total} 篇已蒸馏` : status.value.message
})

const status = ref<any>({ running: false, processed: 0, total: 0, message: '' })
let pollTimer: number | null = null

const loadList = async () => {
  try {
    const res = await http.get('/api/wiki')
    categories.value = res.data.categories || []
    total.value = res.data.total || 0
    running.value = !!res.data.running
  } catch { /* ignore */ }
}

const openPage = async (pageId: string) => {
  try {
    const res = await http.get(`/api/wiki/${pageId}`)
    current.value = res.data
    if (route.path !== `/wiki/${pageId}`) {
      router.replace({ path: `/wiki/${pageId}` })
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载页面失败')
  }
}

const startEdit = () => {
  if (!current.value) return
  editForm.value = {
    content: current.value.content || '',
    summary: current.value.summary || '',
    category: current.value.category || '',
  }
  editing.value = true
}

const saveEdit = async () => {
  if (!current.value) return
  savingEdit.value = true
  try {
    await http.put(`/api/wiki/${current.value.id}`, editForm.value)
    current.value = { ...current.value, ...editForm.value }
    editing.value = false
    ElMessage.success('已保存')
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    savingEdit.value = false
  }
}

const renderContent = (text: string) => {
  if (!text) return ''
  const withLinks = text.replace(/\[\[([^\]]+)\]\]/g, (_, title: string) => {
    const id = titleToId.value[title]
    if (id) {
      return `<a class="wiki-link" href="javascript:void(0)" data-id="${id}">${title}</a>`
    }
    return `<strong>${title}</strong>`
  })
  return md.render(withLinks)
}

const handleContentClick = (e: MouseEvent) => {
  const target = (e.target as HTMLElement).closest('.wiki-link') as HTMLElement | null
  if (target && target.dataset.id) {
    openPage(target.dataset.id)
  }
}

const handleImgError = (e: Event) => {
  const el = e.target as HTMLImageElement
  const src = el.getAttribute('src') || ''
  if (!src.includes('/api/upload/images/proxy') && /^https?:/.test(src)) {
    // First failure: retry once through the no-Referer backend proxy.
    el.src = window.location.origin + '/api/upload/images/proxy?url=' + encodeURIComponent(src)
    return
  }
  el.style.display = 'none'
}

const rebuildWiki = async () => {
  rebuilding.value = true
  try {
    const res = await http.post('/api/wiki/rebuild')
    if (res.data.running) {
      running.value = true
      startPolling()
      ElMessage.info('Wiki 编译已启动，后台进行中')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '启动编译失败')
  } finally {
    rebuilding.value = false
  }
}

const pollStatus = async () => {
  try {
    const res = await http.get('/api/wiki/rebuild-status')
    status.value = res.data
    running.value = !!res.data.running
    if (!res.data.running) {
      stopPolling()
      await loadList()
      if (current.value) {
        const fresh = await http.get(`/api/wiki/${current.value.id}`).catch(() => null)
        if (fresh) current.value = fresh.data
      }
    }
  } catch { /* ignore */ }
}

const startPolling = () => {
  if (pollTimer) return
  pollTimer = window.setInterval(pollStatus, 3000)
}
const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

watch(
  () => route.params.id,
  (id) => {
    if (id) openPage(String(id))
  }
)

onMounted(async () => {
  await loadList()
  const id = route.params.id
  if (id) {
    await openPage(String(id))
  }
  if (running.value) startPolling()
  else await pollStatus()
})

onBeforeUnmount(() => {
  stopPolling()
})
</script>

<style scoped>
@import 'highlight.js/styles/github.css';

.wiki-page {
  height: 100%;
  display: flex;
  background: #f0f2f5;
}
.wiki-sidebar {
  width: 300px;
  background: #fff;
  border-right: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.wiki-sidebar-header {
  padding: 16px 20px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.wiki-brand {
  font-weight: 700;
  font-size: 15px;
  color: #1e293b;
}
.wiki-filter {
  padding: 0 16px 8px;
}
.wiki-cat-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 12px 16px;
}
.wiki-cat {
  margin-bottom: 6px;
}
.wiki-cat-name {
  font-size: 12px;
  font-weight: 600;
  color: #64748b;
  padding: 8px 10px 4px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.wiki-cat-count {
  background: #f1f5f9;
  color: #94a3b8;
  border-radius: 8px;
  padding: 0 6px;
  font-size: 11px;
  margin-left: 4px;
}
.wiki-page-item {
  padding: 7px 12px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 13px;
  color: #334155;
  transition: all 0.12s;
}
.wiki-page-item:hover {
  background: #f1f5f9;
}
.wiki-page-item.active {
  background: #eff6ff;
  color: #2563eb;
  font-weight: 500;
}
.wiki-main {
  flex: 1;
  overflow-y: auto;
  padding: 28px 48px;
}
.wiki-content-card {
  max-width: 860px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
  border: 1px solid #e2e8f0;
  padding: 36px 48px;
  min-height: 70vh;
}
.wiki-card-actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 4px;
}
.wiki-edit-tip {
  font-size: 12px;
  color: #94a3b8;
}
.wiki-edit-field {
  margin-bottom: 8px;
}
.wiki-edit-content :deep(textarea) {
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 13px;
  line-height: 1.6;
}
.wiki-edit-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.wiki-crumb {
  font-size: 12px;
  color: #94a3b8;
  margin-bottom: 8px;
}
.wiki-title {
  font-size: 28px;
  font-weight: 700;
  color: #0f172a;
  margin-bottom: 10px;
}
.wiki-summary {
  background: #f8fafc;
  border-left: 3px solid #3b82f6;
  padding: 10px 14px;
  border-radius: 0 8px 8px 0;
  color: #475569;
  font-size: 13px;
  margin-bottom: 20px;
  line-height: 1.6;
}
.wiki-body {
  font-size: 15px;
  line-height: 1.8;
  color: #1f2937;
}
.wiki-body :deep(h1), .wiki-body :deep(h2), .wiki-body :deep(h3) {
  color: #111827;
  margin: 24px 0 10px;
}
.wiki-body :deep(h1) { font-size: 24px; }
.wiki-body :deep(h2) { font-size: 20px; border-bottom: 1px solid #f1f5f9; padding-bottom: 6px; }
.wiki-body :deep(h3) { font-size: 17px; }
.wiki-body :deep(p) { margin: 10px 0; }
.wiki-body :deep(pre) {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 14px 16px;
  overflow-x: auto;
}
.wiki-body :deep(code) {
  background: #f1f5f9;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}
.wiki-body :deep(pre code) {
  background: none;
  padding: 0;
}
.wiki-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 14px 0;
}
.wiki-body :deep(th), .wiki-body :deep(td) {
  border: 1px solid #e2e8f0;
  padding: 8px 12px;
  text-align: left;
}
.wiki-body :deep(th) { background: #f8fafc; }
.wiki-body :deep(blockquote) {
  border-left: 3px solid #e2e8f0;
  padding-left: 14px;
  color: #64748b;
  margin: 10px 0;
}
.wiki-body :deep(img) {
  max-width: 100%;
  border-radius: 8px;
}
.wiki-link {
  color: #2563eb;
  text-decoration: none;
  border-bottom: 1px dashed #93c5fd;
  cursor: pointer;
}
.wiki-link:hover {
  color: #1d4ed8;
  border-bottom-style: solid;
}
.wiki-sources {
  margin-top: 32px;
  padding-top: 16px;
  border-top: 1px solid #f1f5f9;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.wiki-sources-label {
  font-size: 12px;
  color: #94a3b8;
}
.wiki-source-link {
  font-size: 12px;
  color: #2563eb;
  background: #eff6ff;
  padding: 2px 10px;
  border-radius: 10px;
  text-decoration: none;
}
.wiki-source-link:hover {
  background: #dbeafe;
}
.wiki-empty {
  text-align: center;
  margin-top: 140px;
  color: #94a3b8;
}
.wiki-empty h2 {
  font-size: 18px;
  color: #64748b;
  margin-bottom: 8px;
}
.wiki-progress {
  font-size: 13px;
}
</style>
