<template>
  <div class="page-shell">
    <header class="page-header">
      <div class="ph-main">
        <div class="ph-eyebrow">数据与管道</div>
        <h1 class="page-title">编译管道</h1>
        <p class="page-desc">把笔记本里的笔记编译成知识库页面：知识蒸馏 / 接口文档 / 文档合集 / 变更记录 / 自定义。</p>
      </div>
      <div class="ph-actions">
        <div class="stat-row">
          <div class="stat-item"><span class="stat-num">{{ pipelines.length }}</span><span class="stat-label">管道</span></div>
          <div class="stat-item"><span class="stat-num">{{ enabledCount }}</span><span class="stat-label">已启用</span></div>
          <div class="stat-item"><span class="stat-num">{{ autoCount }}</span><span class="stat-label">自动触发</span></div>
          <div class="stat-item"><span class="stat-num">{{ runningCount }}</span><span class="stat-label">运行中</span></div>
        </div>
        <el-button type="primary" class="btn-new" @click="openCreate">
          <el-icon><Plus /></el-icon><span>新建管道</span>
        </el-button>
      </div>
    </header>

    <div class="page-body">
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">管道列表</div>
        <div class="panel-tools">
          <el-input v-model="q" placeholder="搜索名称或编译方式" clearable class="search">
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-table v-loading="loading" :data="filtered" row-key="id">
        <el-table-column label="名称" min-width="220">
          <template #default="{ row }">
            <div class="cell-line">
              <span class="cell-title">{{ row.name }}</span>
              <span v-if="!row.enabled" class="mini-pill off">停用</span>
            </div>
            <div v-if="row.description" class="cell-sub">{{ row.description }}</div>
          </template>
        </el-table-column>

        <el-table-column label="编译方式" min-width="170">
          <template #default="{ row }">
            <div class="cell-title">{{ KIND_LABELS[row.compiler_kind] || row.compiler_kind }}</div>
            <div v-if="row.template_id" class="cell-sub">模板：{{ templateName(row.template_id) || '—' }}</div>
          </template>
        </el-table-column>

        <el-table-column label="来源笔记本" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="cell-title">{{ sourceNames(row) }}</div>
            <div v-if="row.incremental" class="cell-sub">增量编译</div>
          </template>
        </el-table-column>

        <el-table-column label="目标空间" width="150">
          <template #default="{ row }">
            <div class="cell-title">{{ spaceName(row.target_space_id) }}</div>
            <div v-if="row.auto_trigger" class="cell-sub accent">自动触发</div>
          </template>
        </el-table-column>

        <el-table-column label="最近状态" width="128">
          <template #default="{ row }">
            <span class="st" :class="statusClass(row)">
              <i class="status-dot" :class="statusDot(row)" />{{ statusLabel(row) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="160" align="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-dropdown trigger="click" @command="(m: string) => run(row, m as 'incremental' | 'full')">
                <el-button size="small" type="primary" :loading="row.running" :disabled="!row.enabled">
                  运行<el-icon class="caret"><ArrowDown /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="incremental">增量运行</el-dropdown-item>
                    <el-dropdown-item command="full">全量重编</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <el-dropdown trigger="click" @command="(m: string) => onRowCmd(row, m)">
                <el-button size="small" class="more"><el-icon><MoreHorizontal /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="preview">试编译</el-dropdown-item>
                    <el-dropdown-item command="outputs">查看产出</el-dropdown-item>
                    <el-dropdown-item command="runs">运行记录</el-dropdown-item>
                    <el-dropdown-item command="edit" divided>编辑</el-dropdown-item>
                    <el-dropdown-item command="delete">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <div class="empty-state">
            <div class="empty-title">暂无编译管道</div>
            <div class="empty-sub">点击右上角「新建管道」创建第一条管道</div>
          </div>
        </template>
      </el-table>
    </div>

    <div v-if="runBanner" class="run-banner">
      <i class="status-dot run" /><span>{{ runBanner }}</span>
    </div>
    </div>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="dialog" :title="editing ? '编辑编译管道' : '新建编译管道'" width="640px" top="6vh">
      <el-form label-width="110px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：Qwen 接口文档" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" placeholder="可选" />
        </el-form-item>
        <el-form-item label="编译方式">
          <el-select v-model="form.compiler_kind" style="width: 100%">
            <el-option v-for="(label, key) in KIND_LABELS" :key="key" :label="label" :value="key" />
          </el-select>
        </el-form-item>
        <el-form-item label="编译模板">
          <div class="row-flex">
            <el-select
              v-model="form.template_id"
              clearable
              placeholder="不使用模板（使用内置默认）"
              style="flex: 1"
              @change="onTemplateChange"
            >
              <el-option v-for="t in templateLib" :key="t.id" :label="t.name" :value="t.id" />
            </el-select>
            <el-button @click="router.push('/templates')">管理模板</el-button>
          </div>
          <div class="field-hint">编译的提示词 / 规则 / 输出模板由所选模板决定；未选择时使用内置默认。</div>
        </el-form-item>
        <el-form-item label="来源笔记本">
          <el-select
            v-model="form.notebook_ids"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            style="width: 100%"
            placeholder="选择作为来源的笔记本（可多选）"
          >
            <el-option v-for="n in notebooks" :key="n.id" :label="n.name" :value="n.id" />
          </el-select>
          <div class="field-hint">仅编译所选笔记本内的笔记。</div>
        </el-form-item>
        <el-form-item label="目标空间">
          <el-select v-model="form.target_space_id" clearable placeholder="默认空间" style="width: 100%">
            <el-option v-for="s in spaces" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
          <div class="field-hint">留空 = 默认空间；产物按知识结构生成在该空间内（层级由 LLM 按管道指令决定）。</div>
        </el-form-item>
        <el-form-item label="产出分类">
          <el-input v-model="form.target_category" placeholder="可选：写入知识库的分类标签，如：接口文档" />
        </el-form-item>
        <el-form-item label="自动编译">
          <el-switch v-model="form.auto_trigger" />
          <span class="field-hint inline">笔记变更时自动逐条编译（按笔记限流）</span>
        </el-form-item>
        <el-form-item label="增量编译">
          <el-switch v-model="form.incremental" />
          <span class="field-hint inline">仅编译自上次成功运行后有更新的笔记</span>
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

    <!-- 试编译 -->
    <el-dialog v-model="previewDialog" title="试编译预览" width="760px" top="6vh">
      <div v-if="previewLoading" class="hint">编译中（可能需数秒）…</div>
      <template v-else>
        <div v-if="previewError" class="err">{{ previewError }}</div>
        <template v-else>
          <div class="field-hint">来源笔记本：{{ previewNotebook }}</div>
          <div v-if="previewPlan.length" class="plan">
            <div class="plan-head">拟定产出（新建 / 更新哪些页面）</div>
            <div v-for="(o, i) in previewPlan" :key="i" class="plan-row">
              <span class="act" :class="o.action === 'create' ? 'create' : 'update'">
                {{ o.action === 'create' ? '新建' : '更新' }}
              </span>
              <span class="plan-title">{{ o.title }}</span>
              <span v-if="o.parent" class="plan-parent">父级：{{ o.parent }}</span>
            </div>
          </div>
          <div class="field-hint" style="margin: 10px 0 4px">首个页面正文预览</div>
          <pre class="md-preview">{{ previewContent }}</pre>
        </template>
      </template>
    </el-dialog>

    <!-- 运行记录 -->
    <el-dialog v-model="runsDialog" title="运行记录" width="720px" top="8vh">
      <el-table :data="runs">
        <el-table-column prop="status" label="状态" width="90" />
        <el-table-column label="进度" width="130">
          <template #default="{ row }">{{ row.processed }}/{{ row.total }}（变更 {{ row.changed }}）</template>
        </el-table-column>
        <el-table-column prop="message" label="说明" min-width="220" show-overflow-tooltip />
        <el-table-column prop="started_at" label="开始" width="165" />
        <el-table-column prop="finished_at" label="结束" width="165" />
      </el-table>
    </el-dialog>

    <!-- 产出 -->
    <el-dialog v-model="outputsDialog" title="编译产出（知识库页面）" width="720px" top="8vh">
      <div v-if="outputs.length === 0" class="hint" style="padding: 32px">暂无产出</div>
      <el-table v-else :data="outputs">
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
import { onMounted, onUnmounted, reactive, ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, MoreHorizontal, ArrowDown } from 'lucide-vue-next'
import http from '../api/http'

const router = useRouter()

interface Pipeline {
  id: string
  name: string
  description: string
  scope_type: string
  notebook_ids: string[]
  compiler_kind: string
  template_id?: string | null
  prompt_template: string
  compile_rules: string
  compile_template: string
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
const templateLib = ref<{ id: string; name: string; compiler_kind: string }[]>([])
const loading = ref(false)
const q = ref('')
const dialog = ref(false)
const saving = ref(false)
const editing = ref<Pipeline | null>(null)
const runBanner = ref('')
const form = reactive({
  name: '',
  description: '',
  notebook_ids: [] as string[],
  compiler_kind: 'wiki',
  template_id: '' as string | null,
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
const previewPlan = ref<{ action: string; title: string; parent: string }[]>([])
const previewNotebook = ref('')

const runsDialog = ref(false)
const runs = ref<any[]>([])
const outputsDialog = ref(false)
const outputs = ref<any[]>([])

let timer: ReturnType<typeof setInterval> | undefined

const enabledCount = computed(() => pipelines.value.filter(p => p.enabled).length)
const autoCount = computed(() => pipelines.value.filter(p => p.auto_trigger).length)
const runningCount = computed(() => pipelines.value.filter(p => p.running).length)
const filtered = computed(() => {
  const s = q.value.trim().toLowerCase()
  if (!s) return pipelines.value
  return pipelines.value.filter(p =>
    p.name.toLowerCase().includes(s) ||
    (KIND_LABELS[p.compiler_kind] || p.compiler_kind).toLowerCase().includes(s)
  )
})

function statusLabel(p: Pipeline) {
  if (p.running) return '运行中'
  if (p.last_status === 'success') return '成功'
  if (p.last_status === 'failed') return '失败'
  return '未运行'
}
function statusClass(p: Pipeline) {
  if (p.running) return 'run'
  if (p.last_status === 'success') return 'ok'
  if (p.last_status === 'failed') return 'err'
  return 'idle'
}
function statusDot(p: Pipeline) {
  if (p.running) return 'run'
  if (p.last_status === 'success') return 'ok'
  if (p.last_status === 'failed') return 'err'
  return 'off'
}

function onRowCmd(p: Pipeline, cmd: string) {
  if (cmd === 'preview') preview(p)
  else if (cmd === 'outputs') openOutputs(p)
  else if (cmd === 'runs') openRuns(p)
  else if (cmd === 'edit') openEdit(p)
  else if (cmd === 'delete') removePipeline(p)
}

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

function templateName(id?: string | null) {
  if (!id) return ''
  return templateLib.value.find(t => t.id === id)?.name || ''
}

function sourceNames(p: Pipeline) {
  const ids = p.notebook_ids || []
  if (!ids.length) return '未指定'
  const names = ids.map(id => notebooks.value.find(n => n.id === id)?.name || id)
  return names.join('、')
}

async function loadTemplateLib() {
  try {
    templateLib.value = (await http.get('/api/compile-templates')).data
  } catch {
    templateLib.value = []
  }
}

function onTemplateChange(id: string | null) {
  const t = templateLib.value.find(x => x.id === id)
  if (t) form.compiler_kind = t.compiler_kind
}

function openCreate() {
  editing.value = null
  Object.assign(form, {
    name: '',
    description: '',
    notebook_ids: [],
    compiler_kind: 'wiki',
    template_id: '',
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
    notebook_ids: [...(p.notebook_ids || [])],
    compiler_kind: p.compiler_kind,
    template_id: p.template_id || '',
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
  if (form.notebook_ids.length === 0) {
    ElMessage.warning('请选择至少一个来源笔记本')
    return
  }
  saving.value = true
  try {
    const payload = {
      ...form,
      scope_type: 'notebooks',
      prompt_template: '',
      compile_rules: '',
      compile_template: ''
    }
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
  previewPlan.value = []
  previewNotebook.value = ''
  try {
    const r = await http.post(`/api/pipelines/${p.id}/preview`)
    if (r.data.ok) {
      previewNotebook.value = r.data.notebook
      previewPlan.value = r.data.plan || []
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
  runBanner.value = parts.join('；')
  await load()
}

onMounted(() => {
  load()
  loadNotebooks()
  loadSpaces()
  loadTemplateLib()
  timer = setInterval(refreshRunning, 2500)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.btn-new { display: inline-flex; align-items: center; gap: 6px; }
.search :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--border) inset; }

.cell-line { display: flex; align-items: center; gap: 8px; }
.cell-title { font-size: 13.5px; font-weight: 550; color: var(--text); }
.cell-sub { font-size: 12px; color: var(--text-3); margin-top: 3px; }
.cell-sub.accent { color: var(--primary); }

.mini-pill {
  font-size: 11px;
  font-weight: 500;
  padding: 1px 7px;
  border-radius: 999px;
  white-space: nowrap;
}
.mini-pill.off { color: var(--text-3); background: var(--surface-2); }

.st { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; font-weight: 500; }
.st.ok { color: var(--success); }
.st.err { color: var(--danger); }
.st.run { color: var(--warning); }
.st.idle { color: var(--text-3); }

.row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.more { padding: 5px 8px; }
.caret { margin-left: 3px; }

.run-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
  padding: 10px 14px;
  font-size: 12.5px;
  color: var(--text-2);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.empty-state { padding: 36px 0; }
.empty-title { font-size: 14px; font-weight: 600; color: var(--text-2); }
.empty-sub { font-size: 12.5px; color: var(--text-3); margin-top: 6px; }

.row-flex { display: flex; gap: 8px; width: 100%; }
.field-hint { font-size: 12px; color: var(--text-3); line-height: 1.6; margin-top: 4px; }
.field-hint.inline { margin: 0 0 0 10px; }
.err { color: var(--danger); font-size: 13px; }
.hint { color: var(--text-3); font-size: 13px; padding: 8px 0; }
.link { color: var(--primary); text-decoration: none; }
.link:hover { text-decoration: underline; }

.md-preview {
  max-height: 55vh;
  overflow: auto;
  background: var(--surface-2);
  padding: 14px;
  border-radius: var(--radius);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.plan { border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 12px; max-height: 200px; overflow: auto; }
.plan-head { font-size: 12px; color: var(--text-3); margin-bottom: 8px; }
.plan-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
.act {
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
  flex: 0 0 auto;
}
.act.create { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
.act.update { color: var(--warning); background: color-mix(in srgb, var(--warning) 16%, transparent); }
.plan-title { font-weight: 500; color: var(--text); font-size: 13px; }
.plan-parent { font-size: 12px; color: var(--text-3); }
</style>
