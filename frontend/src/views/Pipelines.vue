<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>编译管道</h2>
        <p class="muted">把笔记本里的笔记编译成 wiki 页（知识蒸馏 / 接口文档 / 文档合集 / 变更记录 / 自定义），产出并入「知识库」浏览。</p>
      </div>
      <el-button type="primary" @click="openCreate">+ 新建管道</el-button>
    </div>

    <div v-if="loading" class="muted">加载中...</div>
    <el-table v-else :data="pipelines" border size="small">
      <el-table-column prop="name" label="名称" min-width="160">
        <template #default="{ row }">
          <span>{{ row.name }}</span>
          <el-tag v-if="!row.enabled" size="small" type="info" style="margin-left: 6px">停用</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="编译方式" width="110">
        <template #default="{ row }">{{ KIND_LABELS[row.compiler_kind] || row.compiler_kind }}</template>
      </el-table-column>
      <el-table-column label="来源" min-width="160">
        <template #default="{ row }">
          <span v-if="row.scope_type === 'all'">全部笔记本</span>
          <span v-else-if="row.scope_type === 'group'">本组全部</span>
          <span v-else>{{ (row.notebook_ids || []).length }} 个笔记本</span>
          <el-tag v-if="row.incremental" size="small" style="margin-left: 6px">增量</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="目标空间" width="150">
        <template #default="{ row }">
          <span>{{ spaceName(row.target_space_id) }}</span>
          <el-tag v-if="row.auto_trigger" size="small" type="success" style="margin-left: 6px">自动</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近状态" width="150">
        <template #default="{ row }">
          <el-tag v-if="row.running" size="small" type="warning">运行中</el-tag>
          <el-tag v-else-if="row.last_status === 'success'" size="small" type="success">成功</el-tag>
          <el-tag v-else-if="row.last_status === 'failed'" size="small" type="danger">失败</el-tag>
          <span v-else class="muted">未运行</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="360">
        <template #default="{ row }">
          <el-dropdown trigger="click" @command="(m: string) => run(row, m as 'incremental' | 'full')">
            <el-button size="small" type="primary" :loading="row.running" :disabled="!row.enabled">
              {{ row.running ? '编译中…' : '运行 ▾' }}
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="incremental">增量运行</el-dropdown-item>
                <el-dropdown-item command="full">全量重编</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button size="small" @click="preview(row)">试编译</el-button>
          <el-button size="small" @click="openOutputs(row)">产出</el-button>
          <el-button size="small" @click="openRuns(row)">记录</el-button>
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" plain @click="removePipeline(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="statusText" class="muted" style="font-size: 12px; margin-top: 8px">{{ statusText }}</div>

    <el-dialog v-model="dialog" :title="editing ? '编辑编译管道' : '新建编译管道'" width="640px">
      <el-form label-width="110px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：Qwen接口文档" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" placeholder="可选" />
        </el-form-item>
        <el-form-item label="编译方式">
          <el-select v-model="form.compiler_kind" style="width: 100%">
            <el-option v-for="(label, key) in KIND_LABELS" :key="key" :label="label" :value="key" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源范围">
          <el-radio-group v-model="form.scope_type">
            <el-radio value="notebooks">指定笔记本</el-radio>
            <el-radio value="group">本组全部</el-radio>
            <el-radio value="all">全部笔记本</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.scope_type === 'notebooks'" label="笔记本">
          <el-select v-model="form.notebook_ids" multiple style="width: 100%" placeholder="选择来源笔记本">
            <el-option v-for="n in notebooks" :key="n.id" :label="n.name" :value="n.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="编译模板">
          <div style="width: 100%">
            <el-input
              v-model="form.prompt_template"
              type="textarea"
              :rows="6"
              :placeholder="defaultTemplate(form.compiler_kind) || '留空=使用内置模板'"
            />
            <div class="muted" style="font-size: 12px; margin-top: 4px; line-height: 1.6">
              用于约束大模型的编译行为（文体、结构、取舍规则）。留空=使用内置模板；
              系统会固定追加「JSON 输出协议 + 父级(parent)规则」，此处只填文体/结构要求。
              <el-button link type="primary" size="small" @click="fillDefaultTemplate">填入内置模板</el-button>
              <el-button link size="small" @click="form.prompt_template = ''">清空</el-button>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="目标空间">
          <el-select v-model="form.target_space_id" clearable placeholder="默认空间" style="width: 100%">
            <el-option v-for="s in spaces" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
          <div class="muted" style="font-size: 12px">留空=默认空间；产物按知识结构生成在该空间内（层级由 LLM 按管道指令决定）。</div>
        </el-form-item>
        <el-form-item label="产出分类">
          <el-input v-model="form.target_category" placeholder="可选：写入知识库的分类标签，如：接口文档" />
        </el-form-item>
        <el-form-item label="自动编译">
          <el-switch v-model="form.auto_trigger" />
          <span class="muted" style="margin-left: 8px; font-size: 12px">笔记变更时自动逐条编译（按笔记限流）</span>
        </el-form-item>
        <el-form-item label="增量编译">
          <el-switch v-model="form.incremental" />
          <span class="muted" style="margin-left: 8px; font-size: 12px">仅编译自上次成功运行后有更新的笔记</span>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="previewDialog" title="试编译预览" width="760px">
      <div v-if="previewLoading" class="muted">编译中（可能需数秒）...</div>
      <template v-else>
        <div v-if="previewError" class="muted" style="color: #f56c6c">{{ previewError }}</div>
        <template v-else>
          <div class="muted" style="font-size: 12px; margin-bottom: 6px">来源笔记本：{{ previewNotebook }}</div>
          <pre class="md-preview">{{ previewContent }}</pre>
        </template>
      </template>
    </el-dialog>

    <el-dialog v-model="runsDialog" title="运行记录" width="720px">
      <el-table :data="runs" size="small" border>
        <el-table-column prop="status" label="状态" width="90" />
        <el-table-column label="进度" width="110">
          <template #default="{ row }">{{ row.processed }}/{{ row.total }}（变更 {{ row.changed }}）</template>
        </el-table-column>
        <el-table-column prop="message" label="说明" min-width="220" show-overflow-tooltip />
        <el-table-column prop="started_at" label="开始" width="170" />
        <el-table-column prop="finished_at" label="结束" width="170" />
      </el-table>
    </el-dialog>

    <el-dialog v-model="outputsDialog" title="编译产出（知识库页）" width="720px">
      <div v-if="outputs.length === 0" class="muted">暂无产出</div>
      <el-table v-else :data="outputs" size="small" border>
        <el-table-column label="标题" min-width="220">
          <template #default="{ row }">
            <router-link class="link" :to="`/wiki/${row.id}`">{{ row.title }}</router-link>
          </template>
        </el-table-column>
        <el-table-column prop="category" label="分类" width="140" />
        <el-table-column prop="updated_at" label="更新时间" width="170" />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import http from '../api/http'

interface Pipeline {
  id: string
  name: string
  description: string
  scope_type: string
  notebook_ids: string[]
  compiler_kind: string
  prompt_template: string
  model: string
  target_category: string
  target_space_id?: string | null
  auto_trigger: boolean
  incremental: boolean
  enabled: boolean
  running: boolean
  last_status: string
}

interface WikiSpaceItem { id: string; name: string; icon: string }

const KIND_LABELS: Record<string, string> = {
  wiki: '知识蒸馏',
  api_doc: '接口文档',
  markdown: '文档合集',
  changelog: '变更记录',
  custom: '自定义'
}

const pipelines = ref<Pipeline[]>([])
const notebooks = ref<{ id: string; name: string }[]>([])
const spaces = ref<WikiSpaceItem[]>([])
const compileTemplates = ref<Record<string, string>>({})
const loading = ref(false)
const dialog = ref(false)
const saving = ref(false)
const editing = ref<Pipeline | null>(null)
const statusText = ref('')
const form = reactive({
  name: '',
  description: '',
  scope_type: 'notebooks',
  notebook_ids: [] as string[],
  compiler_kind: 'wiki',
  prompt_template: '',
  model: '',
  target_category: '',
  target_space_id: '' as string | null,
  auto_trigger: false,
  incremental: true,
  enabled: true
})

const previewDialog = ref(false)
const previewLoading = ref(false)
const previewError = ref('')
const previewContent = ref('')
const previewNotebook = ref('')

const runsDialog = ref(false)
const runs = ref<any[]>([])
const outputsDialog = ref(false)
const outputs = ref<any[]>([])

let timer: ReturnType<typeof setInterval> | undefined

async function load() {
  loading.value = true
  try {
    pipelines.value = (await http.get('/api/pipelines')).data
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

async function loadNotebooks() {
  try {
    const r = await http.get('/api/notebooks')
    notebooks.value = r.data.notebooks || []
  } catch {
    notebooks.value = []
  }
}

async function loadSpaces() {
  try {
    const r = await http.get('/api/wiki/spaces')
    spaces.value = r.data.spaces || []
  } catch {
    spaces.value = []
  }
}

function spaceName(id?: string | null) {
  if (!id) return '默认空间'
  return spaces.value.find(s => s.id === id)?.name || '默认空间'
}

async function loadCompileTemplates() {
  try {
    compileTemplates.value = (await http.get('/api/pipelines/compile-templates')).data.kinds || {}
  } catch {
    compileTemplates.value = {}
  }
}

function defaultTemplate(kind: string) {
  return compileTemplates.value[kind] || ''
}

function fillDefaultTemplate() {
  form.prompt_template = defaultTemplate(form.compiler_kind)
}

function openCreate() {
  editing.value = null
  Object.assign(form, {
    name: '',
    description: '',
    scope_type: 'notebooks',
    notebook_ids: [],
    compiler_kind: 'wiki',
    prompt_template: '',
    model: '',
    target_category: '',
    target_space_id: '',
    auto_trigger: false,
    incremental: true,
    enabled: true
  })
  dialog.value = true
}

function openEdit(p: Pipeline) {
  editing.value = p
  Object.assign(form, {
    name: p.name,
    description: p.description,
    scope_type: p.scope_type,
    notebook_ids: [...(p.notebook_ids || [])],
    compiler_kind: p.compiler_kind,
    prompt_template: p.prompt_template,
    model: p.model,
    target_category: p.target_category,
    target_space_id: p.target_space_id || '',
    auto_trigger: p.auto_trigger,
    incremental: p.incremental,
    enabled: p.enabled
  })
  dialog.value = true
}

async function save() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写名称')
    return
  }
  if (form.scope_type === 'notebooks' && form.notebook_ids.length === 0) {
    ElMessage.warning('请选择至少一个来源笔记本')
    return
  }
  saving.value = true
  try {
    const payload = { ...form }
    if (editing.value) {
      await http.put(`/api/pipelines/${editing.value.id}`, payload)
    } else {
      await http.post('/api/pipelines', payload)
    }
    dialog.value = false
    ElMessage.success('已保存')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function run(p: Pipeline, mode: 'incremental' | 'full' = 'incremental') {
  try {
    await http.post(`/api/pipelines/${p.id}/run`, null, { params: { mode } })
    ElMessage.success(mode === 'full' ? '已开始全量重编' : '已开始增量编译')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '启动失败')
  }
}

async function removePipeline(p: Pipeline) {
  try {
    await ElMessageBox.confirm(`确认删除管道「${p.name}」？（已产出的知识库页保留）`, '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await http.delete(`/api/pipelines/${p.id}`)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function preview(p: Pipeline) {
  previewDialog.value = true
  previewLoading.value = true
  previewError.value = ''
  previewContent.value = ''
  previewNotebook.value = ''
  try {
    const r = await http.post(`/api/pipelines/${p.id}/preview`)
    if (r.data.ok) {
      previewNotebook.value = r.data.notebook
      previewContent.value = r.data.content
    } else {
      previewError.value = r.data.error || '预览失败'
    }
  } catch (e: any) {
    previewError.value = e?.response?.data?.detail || '预览失败'
  } finally {
    previewLoading.value = false
  }
}

async function openRuns(p: Pipeline) {
  runsDialog.value = true
  try {
    runs.value = (await http.get(`/api/pipelines/${p.id}/runs`)).data
  } catch {
    runs.value = []
  }
}

async function openOutputs(p: Pipeline) {
  outputsDialog.value = true
  try {
    outputs.value = (await http.get(`/api/pipelines/${p.id}/pages`)).data
  } catch {
    outputs.value = []
  }
}

async function refreshRunning() {
  const running = pipelines.value.filter((p) => p.running)
  if (running.length === 0) return
  const parts: string[] = []
  for (const p of running) {
    try {
      const s = (await http.get(`/api/pipelines/${p.id}/status`)).data
      parts.push(`${p.name}: ${s.message || '编译中'} (${s.processed}/${s.total})`)
    } catch {
      /* ignore */
    }
  }
  statusText.value = parts.join('；')
  await load()
}

onMounted(() => {
  load()
  loadNotebooks()
  loadSpaces()
  loadCompileTemplates()
  timer = setInterval(refreshRunning, 2500)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.page { padding: 28px 40px 60px; height: 100%; overflow: auto; max-width: 1100px; margin: 0 auto; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; gap: 12px; }
.page-head h2 { margin: 0 0 4px; font-size: 24px; font-weight: 700; color: var(--text); }
.muted { color: var(--text-3); }
.link { color: #409eff; text-decoration: none; }
.md-preview {
  max-height: 60vh; overflow: auto; background: #f7f8fa; padding: 12px;
  border-radius: 8px; font-size: 12px; white-space: pre-wrap; word-break: break-word;
}
</style>
