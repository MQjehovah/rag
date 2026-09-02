<template>
  <div class="app-container">
    <!-- Header -->
    <header class="app-header">
      <div class="search-box">
        <el-input v-model="searchQuery" placeholder="搜索原始资料..." @keyup.enter="doSearch">
          <template #append>
            <el-button @click="doSearch">搜索</el-button>
          </template>
        </el-input>
      </div>
      <div class="header-actions">
        <el-tag type="success" v-if="saveStatus === 'saved'">已保存</el-tag>
        <el-tag type="warning" v-else-if="saveStatus === 'saving'">保存中...</el-tag>
        <el-button @click="goToSources">数据源</el-button>
        <el-button @click="importDialogVisible = true">手动导入</el-button>
        <el-button :loading="organizing" @click="handleOrganize">{{ organizing ? '整理中...' : '自动整理' }}</el-button>
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
               <span class="notebook-icon">📂</span>
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
                   <span class="page-icon">📝</span>
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

          <div class="notebook-item" :class="{ active: currentNotebook?.id === '__unassigned__' }">
            <div class="notebook-info" @click="selectUnassigned">
               <span class="notebook-icon">📋</span>
              <span class="notebook-name">未分类</span>
              <el-tag size="small" type="info" v-if="unassignedPages.length">{{ unassignedPages.length }}</el-tag>
            </div>
            <div v-if="currentNotebook?.id === '__unassigned__'" class="page-list">
              <div
                v-for="page in unassignedPages"
                :key="page.id"
                class="page-item"
                :class="{ active: currentPage?.id === page.id }"
              >
                <div class="page-info" @click="selectPage(page)">
                   <span class="page-icon">📝</span>
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
            </div>
          </div>

          <div v-if="notebooks.length === 0 && unassignedPages.length === 0" class="empty-tip">
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
            :class="{ 'is-readonly': isReadOnlySource }"
            :readonly="isReadOnlySource"
            placeholder="无标题"
            @input="scheduleSave"
          />
          <div v-if="isSourceDocument" class="source-banner">
            <div>
              <el-tag type="info">钉钉同步文档</el-tag>
              <el-tag :type="indexStatusType" class="index-status-tag">
                {{ indexStatusText }}
              </el-tag>
              <span v-if="currentPage.source_path" class="source-path">{{ currentPage.source_path }}</span>
            </div>
            <span class="source-protection-tip">
              {{ sourceEditEnabled ? '源码编辑已启用，修改后索引会变为过期' : '原文保护已开启，浏览不会改写 Markdown' }}
            </span>
          </div>
          <el-input
            v-if="isSourceDocument && sourceEditEnabled"
            v-model="currentPage.content"
            type="textarea"
            :autosize="{ minRows: 20 }"
            class="source-editor"
            @input="scheduleSave"
          />
          <MarkdownPreview
            v-else-if="isSourceDocument"
            :content="currentPage.content"
          />
          <TipTapEditor
            v-else
            v-model="currentPage.content"
            @update:modelValue="scheduleSave"
          />
          <div class="editor-footer">
            <span class="editor-hint">
              {{ isReadOnlySource ? '只读预览' : '自动保存' }}
            </span>
            <div class="editor-actions">
              <el-button
                v-if="isSourceDocument && !sourceEditEnabled"
                size="small"
                @click="enableSourceEdit"
              >启用源码编辑</el-button>
              <el-button
                v-if="isSourceDocument && sourceEditEnabled"
                size="small"
                @click="cancelSourceEdit"
              >取消编辑</el-button>
              <el-button
                v-if="currentPage.source_url"
                size="small"
                @click="openSourcePage"
              >查看钉钉原文</el-button>
              <el-button
                size="small"
                @click="reindexCurrentPage"
                :loading="indexing"
                :disabled="isSourceDocument && currentPage.index_status === 'stale' && !sourceEditEnabled"
              >重新索引</el-button>
            </div>
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

    <!-- 导入知识对话框 -->
    <el-dialog v-model="importDialogVisible" title="手动导入" width="600px">
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

    <!-- J-2：原始资料搜索结果（知识中心内，复用后端 Search 服务） -->
    <el-dialog v-model="rawSearchVisible" title="原始资料搜索结果" width="720px">
      <div v-if="rawSearchLoading" v-loading="true" style="min-height: 120px"></div>
      <div v-else-if="!rawSearchResults.length" class="empty-tip">未找到匹配的原始资料</div>
      <div v-else class="raw-search-list">
        <div v-for="r in rawSearchResults" :key="r.chunk_id" class="raw-search-item">
          <div class="raw-search-title">
            <a :href="r.link">{{ r.title }}</a>
            <el-tag v-if="r.source_type" size="small">{{ r.source_type }}</el-tag>
          </div>
          <div class="raw-search-content">{{ r.content }}</div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import type { UploadFile as ElUploadFile } from 'element-plus'
import http from '../api/http'
import { searchV2 } from '../api/knowledge'
import type { RawResultView } from '../api/ragChat'
import TipTapEditor from '../components/TipTapEditor.vue'
import MarkdownPreview from '../components/MarkdownPreview.vue'

const router = useRouter()

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
  source_type?: string | null
  source_id?: string | null
  source_url?: string | null
  source_path?: string | null
  source_content_hash?: string | null
  content_hash?: string | null
  current_content_hash?: string
  indexed_content_hash?: string | null
  index_status?: 'current' | 'stale' | 'missing' | 'empty'
  last_synced_at?: string | null
  updated_at: string
}

const notebooks = ref<Notebook[]>([])
const notebookPages = ref<PageListItem[]>([])
const currentNotebook = ref<Notebook | null>(null)
const currentPage = ref<Page | null>(null)
const saveStatus = ref<'saved' | 'saving' | 'unsaved'>('saved')
const sourceEditEnabled = ref(false)
const loadedSourceSnapshot = ref<{ title: string; content: string } | null>(null)
const isSourceDocument = computed(() => currentPage.value?.source_type === 'dingtalk')
const isReadOnlySource = computed(() => isSourceDocument.value && !sourceEditEnabled.value)
const indexStatusText = computed(() => ({
  current: '索引已同步',
  stale: '索引已过期',
  missing: '尚未索引',
  empty: '正文为空',
}[currentPage.value?.index_status || 'missing']))
const indexStatusType = computed(() => ({
  current: 'success',
  stale: 'warning',
  missing: 'danger',
  empty: 'info',
}[currentPage.value?.index_status || 'missing'] as 'success' | 'warning' | 'danger' | 'info'))

const searchQuery = ref('')
const showNewNotebook = ref(false)
const newNotebookName = ref('')
const importDialogVisible = ref(false)
const importTab = ref('file')
const importUrl = ref('')
const importText = ref('')
const selectedFile = ref<File | null>(null)
const importLoading = ref(false)
const confirmDialogVisible = ref(false)
const confirmLoading = ref(false)
const confirmForm = ref<{ should_save: boolean; action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; summary: string; content: string; notebooks: { id: string; name: string }[]; pages: { id: string; title: string }[] }>({
  should_save: true, action: 'create_note', title: '', notebook_id: null, new_notebook_name: '', update_page_id: null, summary: '', content: '', notebooks: [], pages: [],
})
const organizing = ref(false)

let saveTimeout: number | null = null
const indexing = ref(false)
const currentPageNum = ref(1)
const totalPages = ref(0)
const pageSize = 50

const hasMorePages = ref(false)
const unassignedPages = ref<PageListItem[]>([])

const loadUnassignedPages = async () => {
  try {
    const res = await http.get('/api/pages', { params: { page: 1, page_size: 50 } })
    unassignedPages.value = res.data.items.filter((p: PageListItem) => !p.notebook_id)
  } catch { /* ignore */ }
}

const selectUnassigned = async () => {
  if (currentNotebook.value?.id === '__unassigned__') {
    currentNotebook.value = null
    return
  }
  currentNotebook.value = { id: '__unassigned__', name: '未分类' }
  await loadUnassignedPages()
}

const loadNotebooks = async () => {
  try {
    const res = await http.get('/api/notebooks')
    notebooks.value = res.data
    loadUnassignedPages()
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
    sourceEditEnabled.value = false
    loadedSourceSnapshot.value = res.data.source_type === 'dingtalk'
      ? { title: res.data.title, content: res.data.content }
      : null
    saveStatus.value = 'saved'
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
      loadUnassignedPages()
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
  if (isReadOnlySource.value) return
  saveStatus.value = 'unsaved'
  if (saveTimeout) clearTimeout(saveTimeout)
  saveTimeout = window.setTimeout(() => savePage(), 1000)
}

const savePage = async () => {
  if (!currentPage.value) return
  saveStatus.value = 'saving'
  try {
    const res = await http.put(`/api/pages/${currentPage.value.id}`, {
      title: currentPage.value.title,
      content: currentPage.value.content,
      allow_source_edit: isSourceDocument.value && sourceEditEnabled.value,
    })
    currentPage.value = res.data
    if (isSourceDocument.value) {
      loadedSourceSnapshot.value = {
        title: res.data.title,
        content: res.data.content,
      }
    }
    saveStatus.value = 'saved'
    const idx = notebookPages.value.findIndex(p => p.id === currentPage.value!.id)
    if (idx >= 0) {
      notebookPages.value[idx] = { ...notebookPages.value[idx], title: res.data.title }
    }
  } catch (e) {
    ElMessage.error('保存失败')
    saveStatus.value = 'unsaved'
  }
}

const reindexCurrentPage = async () => {
  if (!currentPage.value) return
  if (isSourceDocument.value && currentPage.value.index_status === 'stale' && !sourceEditEnabled.value) {
    ElMessage.warning('当前同步正文与索引不一致，请先从钉钉重新同步，避免把错误正文写入向量库')
    return
  }
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

const enableSourceEdit = async () => {
  if (!currentPage.value || !isSourceDocument.value) return
  try {
    await ElMessageBox.confirm(
      '启用后将直接编辑同步文档的 Markdown 源码，修改不会覆盖钉钉原文件，但会使当前向量索引过期。是否继续？',
      '启用源码编辑',
      { confirmButtonText: '继续编辑', cancelButtonText: '保持只读', type: 'warning' },
    )
    loadedSourceSnapshot.value = {
      title: currentPage.value.title,
      content: currentPage.value.content,
    }
    sourceEditEnabled.value = true
  } catch {
    // 用户取消时保持只读。
  }
}

const cancelSourceEdit = () => {
  if (!currentPage.value || !loadedSourceSnapshot.value) return
  if (saveTimeout) clearTimeout(saveTimeout)
  currentPage.value.title = loadedSourceSnapshot.value.title
  currentPage.value.content = loadedSourceSnapshot.value.content
  sourceEditEnabled.value = false
  saveStatus.value = 'saved'
}

const openSourcePage = () => {
  if (!currentPage.value?.source_url) return
  window.open(currentPage.value.source_url, '_blank', 'noopener,noreferrer')
}

// 统一数据源入口：直接跳转数据源页，聚焦钉钉连接。
const goToSources = () => {
  router.push('/sources?connector=dingtalk')
}

const rawSearchVisible = ref(false)
const rawSearchLoading = ref(false)
const rawSearchResults = ref<RawResultView[]>([])

const doSearch = async () => {
  // J-2：原始资料搜索留在知识中心内，复用后端 Search 服务（/api/search/v2），
  // 不再跳转旧 /search 顶级入口（该入口已删除）。
  const q = searchQuery.value.trim()
  if (!q) return
  rawSearchLoading.value = true
  rawSearchVisible.value = true
  rawSearchResults.value = []
  try {
    const res = await searchV2(q)
    rawSearchResults.value = res.raw_results
  } catch {
    rawSearchResults.value = []
  } finally {
    rawSearchLoading.value = false
  }
}

const handleFileChange = (file: ElUploadFile) => {
  selectedFile.value = file.raw || null
}

const _executeSave = async (form: { action: string; title: string; notebook_id: string | null; new_notebook_name: string; update_page_id: string | null; content: string }) => {
  const action = form.action || 'create_note'
  if (action === 'create_notebook' && form.new_notebook_name) {
    const nbRes = await http.post('/api/notebooks', { name: form.new_notebook_name })
    await http.post('/api/pages', { title: form.title, content: form.content, notebook_id: nbRes.data.id })
  } else if (action === 'update_note' && form.update_page_id) {
    await http.put(`/api/pages/${form.update_page_id}`, { content: form.content })
  } else {
    await http.post('/api/pages', { title: form.title, content: form.content, notebook_id: form.notebook_id || undefined })
  }
}

const handleImport = async () => {
  if (importTab.value === 'file' && !selectedFile.value) { ElMessage.warning('请选择文件'); return }
  if (importTab.value === 'url' && !importUrl.value.trim()) { ElMessage.warning('请输入URL'); return }
  if (importTab.value === 'text' && !importText.value.trim()) { ElMessage.warning('请输入文本'); return }
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
    loadNotebooks()
  } catch (e: any) {
    ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    confirmLoading.value = false
  }
}

const handleOrganize = async () => {
  organizing.value = true
  try {
    const token = localStorage.getItem('token')
    const resp = await fetch('/api/organize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    })
    if (!resp.ok) {
      const err = await resp.json()
      ElMessage.error(err.detail || '整理失败')
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
        try {
          const data = JSON.parse(line.slice(6))
          if (data.type === 'progress') ElMessage.info(data.message)
          else if (data.type === 'done') {
            const s = data.stats
            ElNotification({ title: '整理完成', message: `移动 ${s.moved} 篇 | 新建 ${s.created_notebooks} 个笔记本 | 更新 ${s.updated} 篇`, type: 'success' })
            loadNotebooks()
          } else if (data.type === 'error') ElMessage.error(data.content)
        } catch { /* skip */ }
      }
    }
  } catch (e: any) {
    ElMessage.error('整理失败: ' + e.message)
  } finally {
    organizing.value = false
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
.app-container { height: 100%; min-height: 0; display: flex; flex-direction: column; background: #f0f2f5; }
.app-header {
  height: 56px;
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
}
.search-box { width: 400px; }
.search-box :deep(.el-input__wrapper) {
  border-radius: 10px;
  background: #f8fafc;
  box-shadow: none;
  border: 1px solid #e2e8f0;
}
.header-actions { display: flex; align-items: center; gap: 10px; }
.app-body { flex: 1; min-height: 0; display: flex; overflow: hidden; }
.sidebar {
  width: 280px;
  min-height: 0;
  background: #fff;
  border-right: 1px solid #e2e8f0;
  display: flex;
  flex-direction: column;
}
.sidebar-header {
  padding: 16px 20px;
  font-weight: 600;
  font-size: 14px;
  color: #1e293b;
  border-bottom: 1px solid #e2e8f0;
  letter-spacing: -0.2px;
}
.notebook-list { flex: 1; min-height: 0; overflow-y: auto; padding: 8px; }
.notebook-list::-webkit-scrollbar { width: 4px; }
.notebook-list::-webkit-scrollbar-thumb { background: #e2e8f0; border-radius: 2px; }
.notebook-item { margin-bottom: 2px; }
.notebook-info {
  display: flex;
  align-items: center;
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}
.notebook-info:hover { background: #f1f5f9; }
.notebook-item.active > .notebook-info { background: #eff6ff; color: #2563eb; }
.notebook-icon { margin-right: 8px; font-size: 15px; }
.notebook-name { flex: 1; font-weight: 500; font-size: 14px; }
.notebook-info .el-button { margin-left: auto; }
.page-list { padding-left: 16px; }
.page-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
}
.page-item:hover { background: #f1f5f9; }
.page-item.active { background: #eff6ff; }
.page-info { display: flex; align-items: center; flex: 1; min-width: 0; }
.page-icon { margin-right: 6px; font-size: 13px; }
.page-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: #475569;
}
.page-item.active .page-title { color: #1e40af; font-weight: 500; }
.page-menu-btn { opacity: 0; padding: 0 4px; }
.page-item:hover .page-menu-btn { opacity: 1; }
.add-page {
  padding: 7px 12px;
  color: #3b82f6;
  cursor: pointer;
  font-size: 13px;
  border-radius: 6px;
  transition: background 0.15s;
}
.add-page:hover { background: #eff6ff; }
.empty-tip { text-align: center; color: #94a3b8; padding: 30px 20px; font-size: 13px; }
.main-content { flex: 1; min-height: 0; padding: 24px 40px; overflow-y: auto; }
.editor-wrapper {
  max-width: 900px;
  margin: 0 auto;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
  border: 1px solid #e2e8f0;
  min-height: calc(100vh - 104px);
  padding: 32px 44px;
}
.title-input {
  width: 100%;
  font-size: 26px;
  font-weight: 700;
  border: none;
  outline: none;
  padding: 8px 0;
  margin-bottom: 20px;
  color: #0f172a;
  letter-spacing: -0.3px;
}
.title-input::placeholder { color: #cbd5e1; }
.editor-footer {
  margin-top: 28px;
  padding-top: 16px;
  border-top: 1px solid #f1f5f9;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.editor-actions { display: flex; align-items: center; gap: 8px; }
.title-input.is-readonly { color: #334155; cursor: default; }
.source-banner {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 14px;
  margin-bottom: 18px;
  border: 1px solid #dbeafe;
  border-radius: 8px;
  background: #f8fbff;
  color: #64748b;
  font-size: 13px;
}
.index-status-tag { margin-left: 8px; }
.source-path { margin-left: 10px; }
.source-protection-tip { text-align: right; }
.source-editor { margin-bottom: 16px; font-family: Consolas, 'Courier New', monospace; }
.editor-hint { color: #94a3b8; font-size: 13px; }
.empty-state { text-align: center; color: #94a3b8; margin-top: 120px; }
.empty-state h2 { font-size: 20px; color: #475569; margin-bottom: 8px; }
.search-result {
  padding: 16px;
  border-bottom: 1px solid #f1f5f9;
  cursor: pointer;
  border-radius: 8px;
  transition: background 0.15s;
}
.search-result:hover { background: #f8fafc; }
.result-title { font-weight: 600; margin-bottom: 6px; color: #1e293b; }
.result-content { color: #64748b; font-size: 13px; margin-bottom: 8px; line-height: 1.5; }
.result-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.result-footer { margin-top: 6px; }
.save-summary { font-size: 13px; color: #475569; background: #f8fafc; padding: 10px 14px; border-radius: 8px; line-height: 1.5; }
.upload-tip { font-size: 12px; color: #94a3b8; margin-top: 4px; }
.raw-search-list { display: flex; flex-direction: column; gap: 8px; }
.raw-search-item { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 12px; }
.raw-search-title { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.raw-search-title a { font-size: 13px; font-weight: 600; color: #2563eb; text-decoration: none; }
.raw-search-content { font-size: 13px; color: #4b5563; white-space: pre-wrap; }
</style>
