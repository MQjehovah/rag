<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2>嵌入模型</h2>
        <p class="muted">配置可用的 embedding 模型档案；笔记本可指定其一，未指定则用默认档案。</p>
      </div>
      <el-button v-if="isAdmin" type="primary" @click="openCreate">+ 新增档案</el-button>
    </div>

    <el-alert
      v-if="!isAdmin"
      type="info"
      :closable="false"
      show-icon
      title="仅管理员可新增/修改档案；你可以查看当前可用模型。"
      style="margin-bottom: 12px"
    />

    <div v-if="loading" class="muted">加载中...</div>
    <el-table v-else :data="profiles" border size="small">
      <el-table-column prop="name" label="名称" min-width="140">
        <template #default="{ row }">
          <span>{{ row.name }}</span>
          <el-tag v-if="row.is_default" size="small" type="success" style="margin-left: 6px">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="kind" label="类型" width="90" />
      <el-table-column prop="api_url" label="接口地址" min-width="240" show-overflow-tooltip />
      <el-table-column prop="model" label="模型" min-width="180" show-overflow-tooltip />
      <el-table-column prop="dimensions" label="维度" width="80" />
      <el-table-column v-if="isAdmin" label="操作" width="290">
        <template #default="{ row }">
          <el-button size="small" @click="testProfile(row)">测试</el-button>
          <el-button size="small" :disabled="row.is_default" @click="makeDefault(row)">设为默认</el-button>
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" type="danger" plain @click="removeProfile(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="isAdmin" class="reindex panel">
      <div class="reindex-head">
        <div>
          <strong>全库重建向量</strong>
          <div class="muted" style="font-size: 12px">
            按各笔记本指定的档案重新嵌入所有笔记（切换模型后需要重建）。
          </div>
        </div>
        <el-button type="warning" :loading="reindex.running" @click="startReindex">
          {{ reindex.running ? '重建中...' : '开始重建' }}
        </el-button>
      </div>
      <el-progress
        v-if="reindex.total > 0"
        :percentage="Math.round((reindex.processed / reindex.total) * 100)"
        :format="() => `${reindex.processed}/${reindex.total}（失败 ${reindex.errors}）`"
        style="margin-top: 10px"
      />
      <div v-if="reindex.message" class="muted" style="font-size: 12px; margin-top: 6px">{{ reindex.message }}</div>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑嵌入档案' : '新增嵌入档案'" width="560px">
      <el-form label-width="110px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：bge-large-zh" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="form.kind" style="width: 100%">
            <el-option label="OpenAI 兼容 (/v1/embeddings)" value="openai" />
            <el-option label="Ollama (/api/embed)" value="ollama" />
          </el-select>
        </el-form-item>
        <el-form-item label="接口地址">
          <el-input v-model="form.api_url" placeholder="如 http://192.168.31.34:3100/v1/embeddings" />
        </el-form-item>
        <el-form-item label="模型">
          <el-input v-model="form.model" placeholder="如 bge-large-zh-v1.5" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" placeholder="留空不鉴权；编辑时留空表示不改" show-password />
        </el-form-item>
        <el-form-item label="向量维度">
          <el-input-number v-model="form.dimensions" :min="1" :max="8192" />
          <span class="muted" style="margin-left: 8px; font-size: 12px">1024 维可用 pgvector 索引，其它维度走精确检索</span>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import http from '../api/http'
import { useAuthStore } from '../stores/auth'
import { PERM } from '../constants/perms'

interface Profile {
  id: string
  name: string
  kind: string
  api_url: string
  model: string
  dimensions: number
  is_default: boolean
}

const auth = useAuthStore()
const isAdmin = computed(() => auth.hasPerm(PERM.embedding))
const profiles = ref<Profile[]>([])
const loading = ref(false)
const dialog = ref(false)
const saving = ref(false)
const editing = ref<Profile | null>(null)
const form = reactive({
  name: '',
  kind: 'openai',
  api_url: '',
  model: '',
  api_key: '',
  dimensions: 1024,
  is_default: false
})
const reindex = ref({ running: false, processed: 0, total: 0, errors: 0, message: '' })
let timer: ReturnType<typeof setInterval> | undefined

async function load() {
  loading.value = true
  try {
    profiles.value = (await http.get('/api/embeddings/profiles')).data
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, {
    name: '',
    kind: 'openai',
    api_url: '',
    model: '',
    api_key: '',
    dimensions: 1024,
    is_default: profiles.value.length === 0
  })
  dialog.value = true
}

function openEdit(p: Profile) {
  editing.value = p
  Object.assign(form, {
    name: p.name,
    kind: p.kind,
    api_url: p.api_url,
    model: p.model,
    api_key: '',
    dimensions: p.dimensions,
    is_default: p.is_default
  })
  dialog.value = true
}

async function save() {
  if (!form.name.trim() || !form.api_url.trim() || !form.model.trim()) {
    ElMessage.warning('名称 / 接口地址 / 模型为必填')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      const payload: Record<string, unknown> = {
        name: form.name,
        kind: form.kind,
        api_url: form.api_url,
        model: form.model,
        dimensions: form.dimensions
      }
      if (form.api_key) payload.api_key = form.api_key
      await http.put(`/api/embeddings/profiles/${editing.value.id}`, payload)
    } else {
      await http.post('/api/embeddings/profiles', { ...form })
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

async function makeDefault(p: Profile) {
  try {
    await http.post(`/api/embeddings/profiles/${p.id}/default`)
    ElMessage.success(`已将「${p.name}」设为默认`)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

async function testProfile(p: Profile) {
  try {
    // 长同步接口，客户端不设超时
    const r = await http.post(`/api/embeddings/profiles/${p.id}/test`, null, { timeout: 0 })
    if (r.data.ok) {
      ElMessage.success(`连通，返回维度 ${r.data.dimensions}${r.data.match ? '' : '（与配置维度不一致）'}`)
    } else {
      ElMessage.error(r.data.error || '测试失败')
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '测试失败')
  }
}

async function removeProfile(p: Profile) {
  try {
    await ElMessageBox.confirm(`确认删除档案「${p.name}」？`, '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await http.delete(`/api/embeddings/profiles/${p.id}`)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function loadReindex() {
  try {
    reindex.value = (await http.get('/api/embeddings/reindex-status')).data
  } catch {
    /* ignore */
  }
}

async function startReindex() {
  try {
    await http.post('/api/embeddings/reindex')
    await loadReindex()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '启动失败')
  }
}

onMounted(async () => {
  await auth.fetchMe().catch(() => {})
  load()
  loadReindex()
  timer = setInterval(loadReindex, 3000)
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
.reindex { margin-top: 18px; padding: 16px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.reindex-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.panel { }
</style>
