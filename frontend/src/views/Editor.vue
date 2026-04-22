<template>
  <div class="app-container">
    <!-- Header -->
    <header class="app-header">
      <div class="search-box">
        <el-input v-model="searchQuery" placeholder="搜索笔记..." @keyup.enter="doSearch">
          <template #append>
            <el-button @click="doSearch">搜索</el-button>
          </template>
        </el-input>
      </div>
      <div class="header-actions">
        <el-tag type="success" v-if="saveStatus === 'saved'">已保存</el-tag>
        <el-tag type="warning" v-else-if="saveStatus === 'saving'">保存中...</el-tag>
        <el-button @click="$router.push('/graph')">知识图谱</el-button>
        <el-button type="primary" @click="showNewNotebook = true">新建笔记本</el-button>
      </div>
    </header>

    <div class="app-body">
      <!-- 侧边栏 -->
      <aside class="sidebar">
        <div class="sidebar-header">
          <span>笔记本</span>
        </div>

        <div class="notebook-list">
          <div
            v-for="nb in notebooks"
            :key="nb.id"
            class="notebook-item"
            :class="{ active: currentNotebook?.id === nb.id }"
          >
            <div class="notebook-info" @click="selectNotebook(nb)">
              <span class="notebook-icon">📁</span>
              <span class="notebook-name">{{ nb.name }}</span>
              <el-dropdown trigger="click" @command="(cmd: string) => handleNotebookCmd(cmd, nb)">
                <el-button size="small" text>⋮</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="delete">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>

            <div v-if="currentNotebook?.id === nb.id" class="page-list">
              <div
                v-for="page in notebookPages"
                :key="page.id"
                class="page-item"
                :class="{ active: currentPage?.id === page.id }"
              >
                <div class="page-info" @click="selectPage(page)">
                  <span class="page-icon">📄</span>
                  <span class="page-title">{{ page.title || '无标题' }}</span>
                </div>
                <el-dropdown trigger="click" @command="(cmd: string) => handlePageCmd(cmd, page)">
                  <el-button size="small" text class="page-menu-btn">⋮</el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="index">重新索引</el-dropdown-item>
                      <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
              <div v-if="hasMorePages" class="add-page" @click="loadMorePages">
                <span>加载更多...</span>
              </div>
              <div class="add-page" @click="createPage">
                <span>+ 添加笔记</span>
              </div>
            </div>
          </div>

          <div v-if="notebooks.length === 0" class="empty-tip">
            暂无笔记本
          </div>
        </div>
      </aside>

      <!-- 主编辑区 -->
      <main class="main-content">
        <div v-if="currentPage" class="editor-wrapper">
          <input
            v-model="currentPage.title"
            class="title-input"
            placeholder="无标题"
            @input="scheduleSave"
          />
          <TipTapEditor v-model="currentPage.content" @update:modelValue="scheduleSave" />
          <div class="editor-footer">
            <span class="editor-hint">自动保存</span>
            <el-button size="small" @click="reindexCurrentPage" :loading="indexing">重新索引</el-button>
          </div>
        </div>
        <div v-else class="empty-state">
          <h2>欢迎使用笔记系统</h2>
          <p>选择左侧笔记本或创建新笔记本</p>
        </div>
      </main>
    </div>

    <!-- 新建笔记本对话框 -->
    <el-dialog v-model="showNewNotebook" title="新建笔记本" width="400px">
      <el-input v-model="newNotebookName" placeholder="笔记本名称" @keyup.enter="handleCreateNotebook" />
      <template #footer>
        <el-button @click="showNewNotebook = false">取消</el-button>
        <el-button type="primary" @click="handleCreateNotebook">创建</el-button>
      </template>
    </el-dialog>

    <!-- 搜索结果对话框 -->
    <el-dialog v-model="showSearch" title="搜索结果" width="700px">
      <div v-if="searchResults.length > 0">
        <div v-for="result in searchResults" :key="result.id" class="search-result" @click="openFromSearch(result)">
          <div class="result-header">
            <span class="result-title">{{ result.title }}</span>
            <el-tag size="small" :type="getSourceTagType(result.source)">{{ result.source }}</el-tag>
          </div>
          <div class="result-content">{{ result.content }}</div>
          <div class="result-footer">
            <el-tag size="small" type="info">得分: {{ result.score?.toFixed(3) }}</el-tag>
          </div>
        </div>
      </div>
      <el-empty v-else description="未找到相关笔记" />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import http from '../api/http'
import TipTapEditor from '../components/TipTapEditor.vue'

interface Notebook {
  id: string
  name: string
}

interface PageListItem {
  id: string
  title: string
  notebook_id: string | null
  updated_at: string
}

interface Page {
  id: string
  notebook_id: string | null
  title: string
  content: string
  updated_at: string
}

const notebooks = ref<Notebook[]>([])
const notebookPages = ref<PageListItem[]>([])
const currentNotebook = ref<Notebook | null>(null)
const currentPage = ref<Page | null>(null)
const saveStatus = ref<'saved' | 'saving' | 'unsaved'>('saved')

const searchQuery = ref('')
const showNewNotebook = ref(false)
const newNotebookName = ref('')
const showSearch = ref(false)
const searchResults = ref<any[]>([])

let saveTimeout: number | null = null
const indexing = ref(false)
const currentPageNum = ref(1)
const totalPages = ref(0)
const pageSize = 50

const hasMorePages = ref(false)

const loadNotebooks = async () => {
  try {
    const res = await http.get('/api/notebooks')
    notebooks.value = res.data
  } catch (e) {
    ElMessage.error('加载笔记本失败')
  }
}

const loadPages = async (reset = true) => {
  if (!currentNotebook.value) return
  try {
    const page = reset ? 1 : currentPageNum.value + 1
    const res = await http.get('/api/pages', {
      params: { notebook_id: currentNotebook.value.id, page, page_size: pageSize }
    })
    const data = res.data
    if (reset) {
      notebookPages.value = data.items
      currentPageNum.value = 1
    } else {
      notebookPages.value.push(...data.items)
      currentPageNum.value = page
    }
    totalPages.value = Math.ceil(data.total / pageSize)
    hasMorePages.value = currentPageNum.value < totalPages.value
  } catch (e) {
    ElMessage.error('加载笔记失败')
  }
}

const loadMorePages = () => {
  loadPages(false)
}

const selectNotebook = async (nb: Notebook) => {
  if (currentNotebook.value?.id === nb.id) {
    currentNotebook.value = null
    notebookPages.value = []
    return
  }
  currentNotebook.value = nb
  await loadPages(true)

  if (notebookPages.value.length === 0) {
    await createPage()
  }
}

const selectPage = async (page: PageListItem) => {
  if (saveStatus.value === 'unsaved' && currentPage.value) {
    await savePage()
  }
  try {
    const res = await http.get(`/api/pages/${page.id}`)
    currentPage.value = res.data
  } catch (e) {
    ElMessage.error('加载笔记内容失败')
  }
}

const handleCreateNotebook = async () => {
  if (!newNotebookName.value.trim()) {
    ElMessage.warning('请输入笔记本名称')
    return
  }
  try {
    const res = await http.post('/api/notebooks', { name: newNotebookName.value })
    notebooks.value.unshift(res.data)
    newNotebookName.value = ''
    showNewNotebook.value = false
    currentNotebook.value = res.data
    await createPage()
    ElMessage.success('创建成功')
  } catch (e) {
    ElMessage.error('创建失败')
  }
}

const handleNotebookCmd = async (cmd: string, nb: Notebook) => {
  if (cmd === 'delete') {
    try {
      await http.delete(`/api/notebooks/${nb.id}`)
      ElMessage.success('删除成功')
      if (currentNotebook.value?.id === nb.id) {
        currentNotebook.value = null
        notebookPages.value = []
        currentPage.value = null
      }
      loadNotebooks()
    } catch {
      ElMessage.error('删除失败')
    }
  }
}

const handlePageCmd = async (cmd: string, page: PageListItem) => {
  if (cmd === 'delete') {
    try {
      await http.delete(`/api/pages/${page.id}`)
      ElMessage.success('删除成功')
      notebookPages.value = notebookPages.value.filter(p => p.id !== page.id)
      if (currentPage.value?.id === page.id) {
        currentPage.value = null
      }
    } catch {
      ElMessage.error('删除失败')
    }
  } else if (cmd === 'index') {
    try {
      ElMessage.info('正在索引...')
      await http.post(`/api/pages/${page.id}/index`)
      ElMessage.success('索引完成')
    } catch {
      ElMessage.error('索引失败')
    }
  }
}

const createPage = async () => {
  if (!currentNotebook.value) {
    ElMessage.warning('请先选择笔记本')
    return
  }
  try {
    const res = await http.post('/api/pages', {
      title: '无标题',
      content: '',
      notebook_id: currentNotebook.value.id
    })
    notebookPages.value.unshift({ id: res.data.id, title: res.data.title, notebook_id: res.data.notebook_id, updated_at: res.data.updated_at })
    currentPage.value = res.data
    ElMessage.success('创建成功')
  } catch (e) {
    ElMessage.error('创建失败')
  }
}

const scheduleSave = () => {
  saveStatus.value = 'unsaved'
  if (saveTimeout) clearTimeout(saveTimeout)
  saveTimeout = window.setTimeout(() => savePage(), 1000)
}

const savePage = async () => {
  if (!currentPage.value) return
  saveStatus.value = 'saving'
  try {
    await http.put(`/api/pages/${currentPage.value.id}`, {
      title: currentPage.value.title,
      content: currentPage.value.content
    })
    saveStatus.value = 'saved'
    const idx = notebookPages.value.findIndex(p => p.id === currentPage.value!.id)
    if (idx >= 0) {
      notebookPages.value[idx] = { ...notebookPages.value[idx], title: currentPage.value.title }
    }
  } catch (e) {
    ElMessage.error('保存失败')
    saveStatus.value = 'unsaved'
  }
}

const reindexCurrentPage = async () => {
  if (!currentPage.value) return
  indexing.value = true
  try {
    await http.post(`/api/pages/${currentPage.value.id}/index`)
    ElMessage.success('索引完成')
  } catch {
    ElMessage.error('索引失败')
  } finally {
    indexing.value = false
  }
}

const getSourceTagType = (source: string) => {
  if (source.includes('reranker')) return 'danger'
  if (source.includes('graph')) return 'success'
  if (source.includes('keyword')) return 'warning'
  if (source.includes('vector')) return 'primary'
  return 'info'
}

const doSearch = async () => {
  if (!searchQuery.value.trim()) return
  try {
    const res = await http.post('/api/search', { query: searchQuery.value, top_k: 10 })
    searchResults.value = res.data.results || []
    showSearch.value = true
  } catch (e) {
    ElMessage.error('搜索失败')
  }
}

const openFromSearch = async (result: any) => {
  try {
    const res = await http.get(`/api/pages/${result.id}`)
    const page = res.data
    currentPage.value = page

    const nb = notebooks.value.find(n => n.id === page.notebook_id)
    if (nb && currentNotebook.value?.id !== nb.id) {
      currentNotebook.value = nb
      await loadPages(true)
    }
    showSearch.value = false
  } catch {
    ElMessage.error('打开笔记失败')
  }
}

const handleKeydown = (e: KeyboardEvent) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault()
    if (currentPage.value && saveStatus.value !== 'saving') {
      savePage()
    }
  }
}

onMounted(() => {
  loadNotebooks()
  window.addEventListener('keydown', handleKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; }
.app-container { height: 100vh; display: flex; flex-direction: column; background: #f5f7fa; }
.app-header { height: 60px; background: #fff; border-bottom: 1px solid #e4e7ed; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; }
.search-box { width: 400px; }
.header-actions { display: flex; align-items: center; gap: 15px; }
.app-body { flex: 1; display: flex; overflow: hidden; }
.sidebar { width: 280px; background: #fff; border-right: 1px solid #e4e7ed; display: flex; flex-direction: column; }
.sidebar-header { padding: 15px; font-weight: bold; border-bottom: 1px solid #e4e7ed; }
.notebook-list { flex: 1; overflow-y: auto; padding: 10px; }
.notebook-item { margin-bottom: 8px; }
.notebook-info { display: flex; align-items: center; padding: 10px 12px; border-radius: 6px; cursor: pointer; }
.notebook-info:hover { background: #f5f7fa; }
.notebook-item.active > .notebook-info { background: #ecf5ff; }
.notebook-icon { margin-right: 8px; }
.notebook-name { flex: 1; font-weight: 500; }
.notebook-info .el-button { margin-left: auto; }
.page-list { padding-left: 20px; }
.page-item { display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; border-radius: 6px; cursor: pointer; }
.page-item:hover { background: #f5f7fa; }
.page-item.active { background: #e6f7ff; }
.page-info { display: flex; align-items: center; flex: 1; min-width: 0; }
.page-icon { margin-right: 8px; font-size: 12px; }
.page-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
.page-menu-btn { opacity: 0; padding: 0 4px; }
.page-item:hover .page-menu-btn { opacity: 1; }
.add-page { padding: 8px 12px; color: #409eff; cursor: pointer; font-size: 14px; }
.add-page:hover { background: #f5f7fa; }
.empty-tip { text-align: center; color: #999; padding: 20px; }
.main-content { flex: 1; padding: 20px 40px; overflow-y: auto; }
.editor-wrapper { max-width: 900px; margin: 0 auto; background: #fff; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); min-height: calc(100vh - 100px); padding: 30px 40px; }
.title-input { width: 100%; font-size: 28px; font-weight: 600; border: none; outline: none; padding: 10px 0; margin-bottom: 20px; color: #1f2937; }
.title-input::placeholder { color: #9ca3af; }
.editor-footer { margin-top: 30px; padding-top: 20px; border-top: 1px solid #e5e7eb; display: flex; justify-content: space-between; align-items: center; }
.editor-hint { color: #9ca3af; font-size: 14px; }
.empty-state { text-align: center; color: #999; margin-top: 100px; }
.search-result { padding: 15px; border-bottom: 1px solid #e4e7ed; cursor: pointer; }
.search-result:hover { background: #f5f7fa; }
.result-title { font-weight: bold; margin-bottom: 5px; }
.result-content { color: #666; font-size: 14px; margin-bottom: 8px; }
.result-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 5px;
}
.result-footer {
  margin-top: 5px;
}
</style>
