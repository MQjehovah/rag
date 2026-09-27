<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>编译模板</h2>
        <p class="muted">可单独维护的「编译规则」（提示词 / 规则 / 输出模板）；新建编译管道时选择模板即可。</p>
      </div>
      <el-button type="primary" @click="openCreate">+ 新建模板</el-button>
    </div>

    <div v-if="loading" class="muted">加载中...</div>
    <el-table v-else :data="templates" border size="small">
      <el-table-column prop="name" label="名称" min-width="200" />
      <el-table-column label="编译方式" width="110">
        <template #default="{ row }">{{ KIND_LABELS[row.compiler_kind] || row.compiler_kind }}</template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="300" show-overflow-tooltip />
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" plain @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" :title="editing ? '编辑模板' : '新建模板'" width="760px">
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
          <el-input v-model="form.rules" type="textarea" :rows="6" :placeholder="compileRules || '约束 LLM 的编译行为（会追加在内置通用规则之后）'" />
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
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
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
const dialog = ref(false)
const saving = ref(false)
const editing = ref<Tpl | null>(null)
const form = reactive({ name: '', description: '', compiler_kind: 'wiki', prompt: '', rules: '', template: '' })

const builtinKinds = ref<Record<string, { prompt: string; template: string }>>({})
const compileRules = ref('')

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
  editing.value = t
  Object.assign(form, {
    name: t.name, description: t.description, compiler_kind: t.compiler_kind,
    prompt: t.prompt, rules: t.rules, template: t.template
  })
  dialog.value = true
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
.page { padding: 28px 40px 60px; height: 100%; overflow: auto; max-width: 1100px; margin: 0 auto; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; gap: 12px; }
.page-head h2 { margin: 0 0 4px; font-size: 24px; font-weight: 700; color: var(--text); }
.muted { color: var(--text-3); }
</style>
