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
        <el-button @click="openDingTalkDialog">钉钉同步</el-button>
        <el-button @click="importDialogVisible = true">导入知识</el-button>
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
            <el-tag v-if="result.page_number" size="small" type="info">PDF 第 {{ result.page_number }} 页</el-tag>
            <el-tag v-if="result.content_type === 'image_caption'" size="small" type="warning">图片说明</el-tag>
            <el-tag v-else-if="result.content_type === 'table'" size="small" type="success">表格</el-tag>
          </div>
        </div>
      </div>
      <el-empty v-else description="未找到相关笔记" />
    </el-dialog>

    <!-- 钉钉同步对话框 -->
    <el-dialog v-model="showDingTalk" title="钉钉知识库同步" width="760px" top="5vh" :close-on-click-modal="!syncStatus.running">
      <div v-if="syncStatus.running">
        <el-alert type="info" :closable="false" show-icon>
          <template #title>{{ syncStatus.progress }}</template>
        </el-alert>
        <el-progress :percentage="syncStatus.percent" :format="() => `${syncStatus.percent}%`" style="margin-top: 15px" />
        <div class="dt-stage-line">
          当前阶段：{{ syncStageText }}
          <span v-if="syncStatus.stage_total > 0">（{{ syncStatus.stage_processed }}/{{ syncStatus.stage_total }}）</span>
        </div>
        <div class="dt-stat-grid">
          <div><strong>{{ syncStatus.found }}</strong><span>发现</span></div>
          <div><strong>{{ syncStatus.downloaded }}</strong><span>已下载</span></div>
          <div><strong>{{ syncStatus.converted }}</strong><span>新转换</span></div>
          <div><strong>{{ syncStatus.imported }}</strong><span>写入RAG</span></div>
          <div><strong>{{ syncStatus.skipped }}</strong><span>未变化</span></div>
          <div><strong>{{ syncStatus.errors }}</strong><span>失败</span></div>
        </div>
      </div>

      <div v-else-if="dtStep === 0">
        <el-form label-width="120px">
          <el-form-item label="导入到笔记本">
            <el-input v-model="dtNotebookName" placeholder="笔记本名称（不存在则自动创建）" />
          </el-form-item>
          <el-form-item label="钉钉知识库">
            <el-select v-model="dtSpaceId" clearable filterable placeholder="使用.env中配置的知识库" style="width: 100%" :loading="dtSpacesLoading">
              <el-option v-for="space in dtSpaces" :key="space.id" :label="space.name" :value="space.id" />
            </el-select>
          </el-form-item>
        </el-form>
        <el-alert
          title="全量同步会依次执行：扫描清单 → 下载原文件 → 转换Markdown → 写入RAG。源端缺失文档只会软删除，不会自动清理本地文件或RAG。"
          type="info"
          :closable="false"
          show-icon
        />
        <div v-if="syncStatus.last_sync" class="dt-last-sync">上次同步：{{ formatSyncTime(syncStatus.last_sync) }}</div>
      </div>

      <div v-else-if="dtStep === 1">
        <div class="dt-list-toolbar">
          <el-checkbox v-model="dtSelectAll" @change="toggleSelectAll">全选当前结果 ({{ dtSelectedDocs.length }}/{{ dtDocs.length }})</el-checkbox>
          <el-input v-model="dtFilter" placeholder="搜索文档..." style="width: 220px" clearable size="small" />
        </div>
        <div class="dt-doc-list">
          <el-checkbox-group v-model="dtSelectedIds">
            <div
              v-for="doc in filteredDocs"
              :key="doc.id"
              class="dt-doc-item"
              :title="doc.sync?.conversion_error || ''"
            >
              <el-checkbox :value="doc.id">
                <span class="dt-doc-title">{{ doc.title }}</span>
                <el-tag size="small" type="info" class="dt-inline-tag">{{ doc.extension || 'wiki' }}</el-tag>
                <el-tag size="small" :type="pipelineStatusType(documentPipelineStatus(doc))" class="dt-inline-tag">
                  {{ pipelineStatusText(documentPipelineStatus(doc)) }}
                </el-tag>
                <span class="dt-doc-path">{{ doc.path }}</span>
              </el-checkbox>
            </div>
          </el-checkbox-group>
          <div v-if="dtDocs.length === 0" class="dt-empty">未找到文档</div>
        </div>
      </div>

      <div v-else-if="dtStep === 2">
        <el-result
          :icon="syncStatus.stage === 'failed' ? 'error' : syncStatus.stage === 'interrupted' || syncStatus.errors > 0 ? 'warning' : 'success'"
          :title="syncStatus.stage === 'failed' ? '同步任务失败' : syncStatus.stage === 'interrupted' ? '同步被中断，可断点重试' : syncStatus.errors > 0 ? '同步完成（部分失败）' : '同步完成'"
          :sub-title="syncStatus.progress"
        />
        <div class="dt-stat-grid dt-result-stats">
          <div><strong>{{ syncStatus.downloaded }}</strong><span>下载成功</span></div>
          <div><strong>{{ syncStatus.converted }}</strong><span>新转换</span></div>
          <div><strong>{{ syncStatus.imported }}</strong><span>写入RAG</span></div>
          <div><strong>{{ syncStatus.skipped }}</strong><span>未变化</span></div>
          <div><strong>{{ syncStatus.chunks }}</strong><span>RAG分块</span></div>
          <div><strong>{{ syncStatus.errors }}</strong><span>失败</span></div>
        </div>
        <el-collapse v-if="syncStatus.error_details.length" class="dt-errors">
          <el-collapse-item :title="`查看失败明细（${syncStatus.error_details.length}）`">
            <div v-for="(item, index) in syncStatus.error_details" :key="`${item.document_id}-${index}`" class="dt-error-item">
              <strong>{{ item.name }}</strong>
              <el-tag size="small" type="danger">{{ item.stage }}</el-tag>
              <div>{{ item.error }}</div>
            </div>
          </el-collapse-item>
        </el-collapse>
        <div v-if="syncStatus.inventory.deleted > 0" class="dt-soft-delete-tip">
          本次发现 {{ syncStatus.inventory.deleted }} 份源端缺失文档，已安全标记为软删除；未自动删除本地文件和RAG数据。
        </div>
        <el-alert
          v-if="syncStatus.recoverable"
          class="dt-retry-tip"
          type="warning"
          :closable="false"
          show-icon
          :title="syncStatus.retry_plan.restart ? '上次在生成文档阶段检查点前中断，将重新执行原同步范围' : `可断点重试：下载 ${syncStatus.retry_plan.download}，转换 ${syncStatus.retry_plan.conversion}，RAG ${syncStatus.retry_plan.rag}`"
        />
      </div>

      <template #footer>
        <template v-if="syncStatus.running">
          <el-button @click="showDingTalk = false">后台运行并关闭</el-button>
        </template>
        <template v-else-if="dtStep === 0">
          <el-button @click="showDingTalk = false">取消</el-button>
          <el-button type="primary" @click="fetchDingTalkDocs" :loading="dtLoading">获取文档列表</el-button>
          <el-button type="warning" @click="startFullSync">同步整个知识库</el-button>
        </template>
        <template v-else-if="dtStep === 1">
          <el-button @click="dtStep = 0">返回</el-button>
          <el-button type="primary" @click="startSelectedSync" :disabled="dtSelectedIds.length === 0">同步选中 ({{ dtSelectedIds.length }})</el-button>
        </template>
        <template v-else>
          <el-button @click="dtStep = 0">返回配置</el-button>
          <el-button v-if="syncStatus.recoverable" type="warning" @click="retryFailedSync">重试失败/未完成项</el-button>
          <el-button type="primary" @click="showDingTalk = false">关闭</el-button>
        </template>
      </template>
    </el-dialog>

    <!-- 导入知识对话框 -->
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
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox, ElNotification } from 'element-plus'
import type { UploadFile as ElUploadFile } from 'element-plus'
import http from '../api/http'
import TipTapEditor from '../components/TipTapEditor.vue'
import MarkdownPreview from '../components/MarkdownPreview.vue'

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

interface DingTalkSpace {
  id: string
  name: string
}

interface DingTalkDocument {
  id: string
  title: string
  extension?: string
  path?: string
  sync?: {
    pipeline_status?: string
    source_encryption?: string
    conversion_error?: string
    conversion_warnings?: string[]
  }
  [key: string]: unknown
}

interface DingTalkSyncError {
  stage: string
  document_id: string
  name: string
  error: string
}

interface DingTalkSyncStatus {
  task_id: string | null
  running: boolean
  mode: 'full' | 'selected' | null
  stage: string
  progress: string
  percent: number
  stage_processed: number
  stage_total: number
  total: number
  found: number
  downloaded: number
  download_failed: number
  converted: number
  conversion_failed: number
  imported: number
  skipped: number
  rag_failed: number
  chunks: number
  errors: number
  error_details: DingTalkSyncError[]
  inventory: { discovered: number; moved: number; restored: number; deleted: number }
  recoverable: boolean
  retry_count: number
  parent_task_id: string | null
  interrupted_at: string | null
  retry_plan: { download: number; conversion: number; rag: number; restart: boolean }
  last_sync: string
}

const emptySyncStatus = (): DingTalkSyncStatus => ({
  task_id: null,
  running: false,
  mode: null,
  stage: 'idle',
  progress: '尚未执行同步',
  percent: 0,
  stage_processed: 0,
  stage_total: 0,
  total: 0,
  found: 0,
  downloaded: 0,
  download_failed: 0,
  converted: 0,
  conversion_failed: 0,
  imported: 0,
  skipped: 0,
  rag_failed: 0,
  chunks: 0,
  errors: 0,
  error_details: [],
  inventory: { discovered: 0, moved: 0, restored: 0, deleted: 0 },
  recoverable: false,
  retry_count: 0,
  parent_task_id: null,
  interrupted_at: null,
  retry_plan: { download: 0, conversion: 0, rag: 0, restart: false },
  last_sync: '',
})

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
const showSearch = ref(false)
const searchResults = ref<any[]>([])
const showDingTalk = ref(false)
const dtNotebookName = ref('钉钉知识库')
const dtSpaceId = ref('')
const syncStatus = ref<DingTalkSyncStatus>(emptySyncStatus())
let syncPollTimer: number | null = null
const dtStep = ref(0)
const dtLoading = ref(false)
const dtSpacesLoading = ref(false)
const dtSpaces = ref<DingTalkSpace[]>([])
const dtDocs = ref<DingTalkDocument[]>([])
const dtSelectedIds = ref<string[]>([])
const dtFilter = ref('')

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

const getSourceTagType = (source: string) => {
  if (source.includes('reranker')) return 'danger'
  if (source.includes('graph')) return 'success'
  if (source.includes('keyword')) return 'warning'
  if (source.includes('vector')) return 'primary'
  return 'info'
}

const applySyncStatus = (data: Partial<DingTalkSyncStatus>) => {
  syncStatus.value = {
    ...emptySyncStatus(),
    ...data,
    error_details: data.error_details || [],
    inventory: {
      ...emptySyncStatus().inventory,
      ...(data.inventory || {}),
    },
    retry_plan: {
      ...emptySyncStatus().retry_plan,
      ...(data.retry_plan || {}),
    },
  }
}

const stopSyncPolling = () => {
  if (syncPollTimer) {
    clearInterval(syncPollTimer)
    syncPollTimer = null
  }
}

const pollSyncStatus = async () => {
  try {
    const params = syncStatus.value.task_id ? { task_id: syncStatus.value.task_id } : {}
    const res = await http.get('/api/dingtalk/status', { params })
    applySyncStatus(res.data)
    if (!res.data.running && res.data.task_id) {
      stopSyncPolling()
      dtStep.value = 2
      await loadNotebooks()
      if (!showDingTalk.value) {
        ElNotification({
          title: res.data.stage === 'failed' ? '钉钉同步失败' : '钉钉同步完成',
          message: res.data.progress,
          type: res.data.stage === 'failed' ? 'error' : res.data.errors > 0 ? 'warning' : 'success',
        })
      }
    }
  } catch (e: any) {
    stopSyncPolling()
    ElMessage.error(e?.response?.data?.detail || '获取同步进度失败')
  }
}

const startSyncPolling = () => {
  stopSyncPolling()
  void pollSyncStatus()
  syncPollTimer = window.setInterval(pollSyncStatus, 1500)
}

const loadDingTalkSpaces = async () => {
  dtSpacesLoading.value = true
  try {
    const res = await http.get('/api/dingtalk/spaces')
    dtSpaces.value = Array.isArray(res.data) ? res.data : []
  } catch (e: any) {
    ElMessage.warning(e?.response?.data?.detail || '知识库列表加载失败，可继续使用.env中的默认知识库')
  } finally {
    dtSpacesLoading.value = false
  }
}

const openDingTalkDialog = async () => {
  showDingTalk.value = true
  dtStep.value = 0
  try {
    const res = await http.get('/api/dingtalk/status')
    applySyncStatus(res.data)
    if (res.data.running) startSyncPolling()
    else if (res.data.recoverable && res.data.task_id) dtStep.value = 2
  } catch { /* 由后续具体操作展示错误 */ }
  if (dtSpaces.value.length === 0) void loadDingTalkSpaces()
}

const syncStageText = computed(() => ({
  queued: '等待执行',
  inventory: '扫描文档清单',
  download: '下载原文件',
  conversion: '转换Markdown',
  rag: '写入RAG知识库',
  completed: '已完成',
  failed: '失败',
  interrupted: '进程中断',
  idle: '未开始',
}[syncStatus.value.stage] || syncStatus.value.stage))

const pipelineStatusText = (status?: string) => ({
  imported: '已入库',
  source_changed: '内容已更新',
  converted: '待入库',
  conversion_failed: '转换失败',
  encrypted_source: '源文件已加密',
  downloaded: '待转换',
  download_failed: '下载失败',
  source_deleted: '源端已删除',
  discovered: '待下载',
  new: '新文档',
}[status || 'new'] || status || '新文档')

const pipelineStatusType = (status?: string) => ({
  imported: 'success',
  source_changed: 'warning',
  converted: 'warning',
  conversion_failed: 'danger',
  encrypted_source: 'warning',
  download_failed: 'danger',
  source_deleted: 'info',
}[status || 'new'] || 'info') as 'success' | 'warning' | 'danger' | 'info'

const documentPipelineStatus = (document: DingTalkDocument) => (
  document.sync?.source_encryption
    ? 'encrypted_source'
    : document.sync?.pipeline_status
)

const formatSyncTime = (value: string) => {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

const dtSelectedDocs = computed(() => dtDocs.value.filter((d) => dtSelectedIds.value.includes(d.id)))

const dtSelectAll = computed({
  get: () => filteredDocs.value.length > 0 && filteredDocs.value.every((doc) => dtSelectedIds.value.includes(doc.id)),
  set: () => {},
})

const filteredDocs = computed(() => {
  if (!dtFilter.value) return dtDocs.value
  const q = dtFilter.value.toLowerCase()
  return dtDocs.value.filter((d) =>
    (d.title || '').toLowerCase().includes(q)
    || (d.path || '').toLowerCase().includes(q)
  )
})

const toggleSelectAll = (val: any) => {
  if (val) {
    dtSelectedIds.value = filteredDocs.value.map((d: any) => d.id)
  } else {
    dtSelectedIds.value = []
  }
}

const fetchDingTalkDocs = async () => {
  dtLoading.value = true
  try {
    const params: any = {}
    if (dtSpaceId.value) params.space_id = dtSpaceId.value
    const res = await http.get('/api/dingtalk/docs', { params })
    dtDocs.value = res.data.docs || []
    dtSelectedIds.value = []
    dtStep.value = 1
    if (dtDocs.value.length === 0) {
      ElMessage.warning('未找到文档')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '获取文档列表失败')
  } finally {
    dtLoading.value = false
  }
}

const startSelectedSync = async () => {
  if (dtSelectedIds.value.length === 0) {
    ElMessage.warning('请选择要同步的文档')
    return
  }
  try {
    const res = await http.post('/api/dingtalk/sync-selected', {
      notebook_name: dtNotebookName.value,
      docs: dtSelectedDocs.value,
    })
    applySyncStatus(res.data.status)
    startSyncPolling()
    ElMessage.success(`开始同步 ${dtSelectedIds.value.length} 篇文档`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '启动同步失败')
  }
}

const startFullSync = async () => {
  try {
    await ElMessageBox.confirm(
      '将扫描整个目标知识库，并依次下载原文件、转换Markdown、写入RAG。完整扫描中缺失的文档会被软删除，是否继续？',
      '确认全量同步',
      { confirmButtonText: '开始同步', cancelButtonText: '取消', type: 'warning' },
    )
    const res = await http.post('/api/dingtalk/sync', {
      notebook_name: dtNotebookName.value,
      space_id: dtSpaceId.value || undefined,
    })
    applySyncStatus(res.data.status)
    startSyncPolling()
    ElMessage.success('全量同步已启动')
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e?.response?.data?.detail || '启动全量同步失败')
  }
}

const retryFailedSync = async () => {
  try {
    const plan = syncStatus.value.retry_plan
    const description = plan.restart
      ? '上次任务在生成文档阶段检查点前中断，需要重新执行原同步范围。是否继续？'
      : `系统将按检查点重试：下载 ${plan.download}，转换 ${plan.conversion}，RAG ${plan.rag}。已成功的阶段不会重复执行，是否继续？`
    await ElMessageBox.confirm(
      description,
      '确认断点重试',
      { confirmButtonText: '开始重试', cancelButtonText: '取消', type: 'warning' },
    )
    const res = await http.post('/api/dingtalk/retry', {
      task_id: syncStatus.value.task_id,
    })
    applySyncStatus(res.data.status)
    startSyncPolling()
    ElMessage.success('断点重试已启动')
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e?.response?.data?.detail || '启动重试失败')
  }
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
  void http.get('/api/dingtalk/status').then((res) => {
    applySyncStatus(res.data)
    if (res.data.running) startSyncPolling()
  }).catch(() => {})
  window.addEventListener('keydown', handleKeydown)
})

onBeforeUnmount(() => {
  stopSyncPolling()
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; }
.app-container { height: 100vh; display: flex; flex-direction: column; background: #f0f2f5; }
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
.app-body { flex: 1; display: flex; overflow: hidden; }
.sidebar {
  width: 280px;
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
.notebook-list { flex: 1; overflow-y: auto; padding: 8px; }
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
.main-content { flex: 1; padding: 24px 40px; overflow-y: auto; }
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
.dt-doc-list { max-height: 400px; overflow-y: auto; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px; }
.dt-doc-item { padding: 6px 8px; border-radius: 4px; transition: background 0.15s; }
.dt-doc-item:hover { background: #f8fafc; }
.dt-doc-title { font-size: 13px; font-weight: 500; color: #1e293b; }
.dt-doc-path { font-size: 12px; color: #94a3b8; margin-left: 8px; }
.dt-inline-tag { margin-left: 6px; }
.dt-list-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.dt-empty { text-align: center; color: #94a3b8; padding: 24px; }
.dt-last-sync { color: #64748b; font-size: 12px; margin-top: 12px; }
.dt-stage-line { margin-top: 10px; color: #64748b; font-size: 13px; }
.dt-stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 8px;
  margin-top: 18px;
}
.dt-stat-grid > div {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 12px 6px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
}
.dt-stat-grid strong { color: #1e293b; font-size: 20px; }
.dt-stat-grid span { margin-top: 4px; color: #64748b; font-size: 12px; }
.dt-result-stats { margin-top: 0; }
.dt-errors { margin-top: 18px; }
.dt-error-item { padding: 10px 0; border-bottom: 1px solid #f1f5f9; color: #64748b; font-size: 13px; }
.dt-error-item strong { margin-right: 8px; color: #334155; }
.dt-error-item div { margin-top: 5px; word-break: break-word; }
.dt-soft-delete-tip { margin-top: 16px; padding: 10px 12px; border-radius: 8px; background: #fff7ed; color: #9a3412; font-size: 13px; }
.dt-retry-tip { margin-top: 14px; }
</style>
