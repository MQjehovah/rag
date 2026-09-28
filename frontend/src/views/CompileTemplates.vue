<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h2>编译模板</h2>
        <p class="page-sub">可单独维护的编译规则（提示词 / 规则 / 输出模板）；新建编译管道时选择模板即可复用。</p>
      </div>
      <el-button type="primary" class="btn-new" @click="openCreate">
        <el-icon><Plus /></el-icon><span>新建模板</span>
      </el-button>
    </header>

    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">模板列表</div>
        <div class="panel-tools">
          <el-input v-model="q" placeholder="搜索名称或说明" clearable class="search">
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>

      <el-table v-loading="loading" :data="filtered" row-key="id" @row-click="openView">
        <el-table-column label="名称" min-width="240">
          <template #default="{ row }">
            <div class="cell-title">{{ row.name }}</div>
            <div v-if="row.description" class="cell-sub">{{ row.description }}</div>
          </template>
        </el-table-column>
        <el-table-column label="编译方式" width="130">
          <template #default="{ row }">
            <span class="kind">{{ KIND_LABELS[row.compiler_kind] || row.compiler_kind }}</span>
          </template>
        </el-table-column>
        <el-table-column label="规则构成" min-width="260">
          <template #default="{ row }">
            <div class="seg">
              <span class="seg-tag" :class="{ set: !!row.prompt }">提示词</span>
              <span class="seg-tag" :class="{ set: !!row.rules }">规则</span>
              <span class="seg-tag" :class="{ set: !!row.template }">输出模板</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="right">
          <template #default="{ row }">
            <div class="row-actions" @click.stop>
              <el-button size="small" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" class="more" @click="remove(row)">
                <el-icon><Trash2 /></el-icon>
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-state">
            <div class="empty-title">暂无编译模板</div>
            <div class="empty-sub">点击右上角「新建模板」创建</div>
          </div>
        </template>
      </el-table>
    </div>

    <!-- 查看 -->
    <el-drawer v-model="viewDrawer" :title="viewing?.name" size="560px">
      <div v-if="viewing" class="view">
        <div class="view-meta">
          <span class="kind">{{ KIND_LABELS[viewing.compiler_kind] || viewing.compiler_kind }}</span>
          <span v-if="viewing.description" class="view-desc">{{ viewing.description }}</span>
        </div>
        <div class="seg-block">
          <div class="seg-label">提示词 <span class="seg-hint">角色 + 目标</span></div>
          <pre class="seg-body">{{ viewing.prompt || '（留空，使用内置）' }}</pre>
        </div>
        <div class="seg-block">
          <div class="seg-label">规则 <span class="seg-hint">追加在内置通用规则之后</span></div>
          <pre class="seg-body">{{ viewing.rules || '（留空，仅用内置通用规则）' }}</pre>
        </div>
        <div class="seg-block">
          <div class="seg-label">输出模板 <span class="seg-hint">正文结构骨架</span></div>
          <pre class="seg-body">{{ viewing.template || '（留空，按内容合理分节）' }}</pre>
        </div>
        <div class="view-foot">
          <el-button @click="openEdit(viewing)">编辑此模板</el-button>
        </div>
      </div>
    </el-drawer>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="dialog" :title="editing ? '编辑模板' : '新建模板'" width="760px" top="6vh">
      <el-form label-width="100px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：产品知识库（按产品分根页）" />
        </el-form-item>
        <el-form-item label="编译方式">
          <el-select v-model="form.compiler_kind" style="width: 100%">
            <el-option v-for="(label, key) in KIND_LABELS" :key="key" :label="label" :value="key" />
          </el-select>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.description" placeholder="可选" />
        </el-form-item>
        <el-form-item label="提示词">
          <el-input v-model="form.prompt" type="textarea" :rows="3" :placeholder="defaultPrompt(form.compiler_kind) || '角色与目标'" />
        </el-form-item>
        <el-form-item label="规则">
          <el-input v-model="form.rules" type="textarea" :rows="6" :placeholder="compileRules || '约束 LLM 的编译行为（追加在内置通用规则之后）'" />
        </el-form-item>
        <el-form-item label="输出模板">
          <el-input v-model="form.template" type="textarea" :rows="8" :placeholder="defaultTemplate(form.compiler_kind) || '页面正文结构骨架'" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button @click="fillBuiltin">填入内置</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Trash2 } from 'lucide-vue-next'
import http from '../api/http'

interface Tpl {
  id: string
  name: string
  description: string
  compiler_kind: string
  prompt: string
  rules: string
  template: string
}

const KIND_LABELS: Record<string, string> = {
  wiki: '知识蒸馏',
  api_doc: '接口文档',
  markdown: '文档合集',
  changelog: '变更记录',
  custom: '自定义'
}

const templates = ref<Tpl[]>([])
const loading = ref(false)
const q = ref('')
const dialog = ref(false)
const saving = ref(false)
const editing = ref<Tpl | null>(null)
const form = reactive({ name: '', description: '', compiler_kind: 'wiki', prompt: '', rules: '', template: '' })

const viewDrawer = ref(false)
const viewing = ref<Tpl | null>(null)

const builtinKinds = ref<Record<string, { prompt: string; template: string }>>({})
const compileRules = ref('')

const filtered = computed(() => {
  const s = q.value.trim().toLowerCase()
  if (!s) return templates.value
  return templates.value.filter(t =>
    t.name.toLowerCase().includes(s) ||
    (t.description || '').toLowerCase().includes(s)
  )
})

const defaultPrompt = (kind: string) => builtinKinds.value[kind]?.prompt || ''
const defaultTemplate = (kind: string) => builtinKinds.value[kind]?.template || ''

async function loadBuiltin() {
  try {
    const d = (await http.get('/api/pipelines/compile-templates')).data
    builtinKinds.value = d.kinds || {}
    compileRules.value = d.rules || ''
  } catch { /* ignore */ }
}

async function load() {
  loading.value = true
  try {
    templates.value = (await http.get('/api/compile-templates')).data
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { name: '', description: '', compiler_kind: 'wiki', prompt: '', rules: '', template: '' })
  dialog.value = true
}

function openEdit(t: Tpl) {
  viewing.value = null
  viewDrawer.value = false
  editing.value = t
  Object.assign(form, {
    name: t.name, description: t.description, compiler_kind: t.compiler_kind,
    prompt: t.prompt, rules: t.rules, template: t.template
  })
  dialog.value = true
}

function openView(t: Tpl) {
  viewing.value = t
  viewDrawer.value = true
}

function fillBuiltin() {
  form.prompt = defaultPrompt(form.compiler_kind)
  form.rules = compileRules.value
  form.template = defaultTemplate(form.compiler_kind)
}

async function save() {
  if (!form.name.trim()) { ElMessage.warning('请输入名称'); return }
  saving.value = true
  try {
    if (editing.value) {
      await http.put(`/api/compile-templates/${editing.value.id}`, { ...form })
    } else {
      await http.post('/api/compile-templates', { ...form })
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

async function remove(t: Tpl) {
  try {
    await ElMessageBox.confirm(`确认删除模板「${t.name}」？`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await http.delete(`/api/compile-templates/${t.id}`)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

onMounted(() => {
  loadBuiltin()
  load()
})
</script>

<style scoped>
.page { padding: 32px 40px 64px; height: 100%; overflow: auto; max-width: 1180px; margin: 0 auto; }

.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  padding-bottom: 18px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border);
}
.page-head h2 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: -0.01em; color: var(--text); }
.page-sub { margin: 5px 0 0; font-size: 13px; color: var(--text-3); }
.btn-new { display: inline-flex; align-items: center; gap: 6px; }

.panel-title { font-size: 14px; font-weight: 600; color: var(--text); }
.panel-tools { display: flex; align-items: center; gap: 10px; }
.search :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--border) inset; }

.cell-title { font-size: 13.5px; font-weight: 550; color: var(--text); }
.cell-sub { font-size: 12px; color: var(--text-3); margin-top: 3px; }
.kind { font-size: 12px; color: var(--text-2); }

.seg { display: flex; gap: 6px; flex-wrap: wrap; }
.seg-tag {
  font-size: 11.5px;
  padding: 2px 9px;
  border-radius: 999px;
  color: var(--text-3);
  background: var(--surface-2);
  border: 1px solid transparent;
}
.seg-tag.set { color: var(--primary); background: var(--primary-weak); }

.row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.more { padding: 5px 8px; color: var(--danger); }

.empty-state { padding: 36px 0; }
.empty-title { font-size: 14px; font-weight: 600; color: var(--text-2); }
.empty-sub { font-size: 12.5px; color: var(--text-3); margin-top: 6px; }

.view-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
.view-desc { font-size: 12.5px; color: var(--text-3); }
.seg-block { margin-bottom: 18px; }
.seg-label { font-size: 12.5px; font-weight: 600; color: var(--text-2); margin-bottom: 6px; }
.seg-hint { font-weight: 400; color: var(--text-3); margin-left: 6px; font-size: 11.5px; }
.seg-body {
  margin: 0;
  padding: 12px 14px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--text-2);
}
.view-foot { padding-top: 8px; }
</style>
