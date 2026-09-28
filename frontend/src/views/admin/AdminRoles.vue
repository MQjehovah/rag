<template>
  <div class="page-shell">
    <header class="page-header">
      <div class="ph-main">
        <div class="ph-eyebrow">系统管理</div>
        <h1 class="page-title">角色权限</h1>
        <p class="page-desc">角色是一组权限键的集合：用户绑定角色后即获得对应能力。内置角色只读，不可编辑或删除。</p>
      </div>
      <div class="ph-actions">
        <el-button type="primary" class="btn-new" @click="openCreate">
          <el-icon><Plus /></el-icon><span>新建角色</span>
        </el-button>
      </div>
    </header>

    <div class="page-body">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">角色列表</div>
          <div class="panel-tools">
            <el-button :loading="loading" @click="load">刷新</el-button>
          </div>
        </div>

        <el-table v-loading="loading" :data="roles" row-key="id">
          <el-table-column label="显示名" min-width="200">
            <template #default="{ row }">
              <div class="cell-line">
                <span class="cell-title">{{ row.display_name }}</span>
                <el-tag v-if="row.is_system" size="small" type="info" effect="plain">内置</el-tag>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="标识" width="200">
            <template #default="{ row }">
              <span class="ident">{{ row.name }}</span>
            </template>
          </el-table-column>

          <el-table-column label="权限摘要" min-width="280">
            <template #default="{ row }">
              <span class="perm-summary" :title="permFullText(row)">{{ permSummary(row) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="使用人数" width="110">
            <template #default="{ row }">
              <span class="cell-title">{{ row.user_count }}</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="150" align="right">
            <template #default="{ row }">
              <div class="row-actions">
                <el-button
                  size="small"
                  :disabled="row.is_system"
                  :title="row.is_system ? '内置角色只读' : ''"
                  @click="openEdit(row)"
                >编辑</el-button>
                <el-button
                  size="small"
                  class="more"
                  :disabled="row.is_system"
                  :title="row.is_system ? '内置角色只读' : ''"
                  @click="remove(row)"
                >
                  <el-icon><Trash2 /></el-icon>
                </el-button>
              </div>
            </template>
          </el-table-column>

          <template #empty>
            <div class="empty-state">
              <template v-if="loadError">
                <div class="empty-title">加载失败</div>
                <div class="empty-sub">无法获取角色或权限目录，请稍后重试</div>
                <el-button size="small" style="margin-top: 12px" @click="load">重试</el-button>
              </template>
              <template v-else>
                <div class="empty-title">暂无角色</div>
                <div class="empty-sub">点击右上角「新建角色」创建第一个角色</div>
              </template>
            </div>
          </template>
        </el-table>
      </div>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑角色' : '新建角色'" width="680px" top="6vh">
      <el-form label-width="90px">
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" maxlength="64" placeholder="如：数据源运维" />
        </el-form-item>
        <el-form-item label="标识">
          <el-input v-model="form.name" :disabled="!!editing" maxlength="64" placeholder="如：source_ops" />
          <div v-if="!editing" class="field-hint">唯一标识，用于 API 与用户绑定；创建后不可修改。</div>
        </el-form-item>
        <el-form-item label="权限">
          <div class="perm-box">
            <el-checkbox-group v-model="form.permissions">
              <div v-for="g in catalog" :key="g.group" class="perm-group">
                <div class="perm-group-head">
                  <span class="perm-group-name">{{ g.group }}</span>
                  <span class="perm-group-count">{{ groupSelected(g) }}/{{ g.items.length }}</span>
                  <el-button link size="small" :disabled="saving" @click="selectGroup(g)">全选本组</el-button>
                  <el-button link size="small" :disabled="saving" @click="clearGroup(g)">清空</el-button>
                </div>
                <el-checkbox v-for="it in g.items" :key="it.key" :value="it.key" :disabled="saving" class="perm-item">
                  <span class="perm-item-name">{{ it.name }}</span>
                  <span class="perm-item-desc">{{ it.desc }}</span>
                </el-checkbox>
              </div>
            </el-checkbox-group>
            <div v-if="!catalog.length" class="field-hint">权限目录加载中或为空。</div>
          </div>
          <div class="field-hint">留空表示该角色没有任何管理权限；保存时按勾选项全量提交。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="saving || !catalog.length" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Trash2 } from 'lucide-vue-next'
import http from '../../api/http'

interface PermissionItem {
  key: string
  name: string
  desc: string
}
interface PermissionGroup {
  group: string
  items: PermissionItem[]
}
interface Role {
  id: string
  name: string
  display_name: string
  permissions: string[]
  is_system: boolean
  user_count: number
}

const roles = ref<Role[]>([])
const catalog = ref<PermissionGroup[]>([])
const loading = ref(false)
const loadError = ref(false)
const loadedOnce = ref(false)
const dialog = ref(false)
const saving = ref(false)
const editing = ref<Role | null>(null)
const form = reactive({ name: '', display_name: '', permissions: [] as string[] })

const permNameMap = computed(() => {
  const map: Record<string, string> = {}
  for (const g of catalog.value) for (const it of g.items) map[it.key] = it.name
  return map
})

const catalogKeys = computed(() => new Set(Object.keys(permNameMap.value)))

function permSummary(r: Role): string {
  if (r.permissions.includes('*')) return '全部权限'
  if (r.permissions.length === 0) return '无'
  const names = r.permissions.slice(0, 3).map(k => permNameMap.value[k] || k)
  return r.permissions.length > 3 ? `${names.join('、')} 等 ${r.permissions.length} 项` : names.join('、')
}

function permFullText(r: Role): string {
  if (r.permissions.includes('*')) return '全部权限'
  if (r.permissions.length === 0) return '无'
  return r.permissions.map(k => permNameMap.value[k] || k).join('、')
}

function groupSelected(g: PermissionGroup): number {
  const keys = new Set(g.items.map(i => i.key))
  return form.permissions.filter(k => keys.has(k)).length
}

function selectGroup(g: PermissionGroup) {
  const set = new Set(form.permissions)
  for (const it of g.items) set.add(it.key)
  form.permissions = [...set]
}

function clearGroup(g: PermissionGroup) {
  const keys = new Set(g.items.map(i => i.key))
  form.permissions = form.permissions.filter(k => !keys.has(k))
}

async function load() {
  loading.value = true
  try {
    const [r, c] = await Promise.all([
      http.get('/api/admin/roles'),
      http.get('/api/admin/permissions'),
    ])
    roles.value = r.data.items || []
    catalog.value = c.data || []
    loadedOnce.value = true
    loadError.value = false
  } catch (e: any) {
    if (!loadedOnce.value) {
      roles.value = []
      catalog.value = []
      loadError.value = true
    }
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { name: '', display_name: '', permissions: [] as string[] })
  dialog.value = true
}

function openEdit(r: Role) {
  if (r.is_system) return
  editing.value = r
  Object.assign(form, {
    name: r.name,
    display_name: r.display_name,
    permissions: r.permissions.filter(k => catalogKeys.value.has(k)),
  })
  dialog.value = true
}

async function save() {
  const displayName = form.display_name.trim()
  const name = form.name.trim()
  if (!displayName) {
    ElMessage.warning('请填写显示名')
    return
  }
  if (!editing.value && !name) {
    ElMessage.warning('请填写角色标识')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await http.put(`/api/admin/roles/${editing.value.id}`, {
        display_name: displayName,
        permissions: form.permissions,
      })
    } else {
      await http.post('/api/admin/roles', {
        name,
        display_name: displayName,
        permissions: form.permissions,
      })
    }
    dialog.value = false
    ElMessage.success('已保存')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  } finally {
    saving.value = false
  }
}

async function remove(r: Role) {
  try {
    await ElMessageBox.confirm(
      `确认删除角色「${r.display_name}」？当前 ${r.user_count} 个用户使用该角色。`,
      '确认',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await http.delete(`/api/admin/roles/${r.id}`)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '操作失败')
  }
}

onMounted(load)
</script>

<style scoped>
.btn-new { display: inline-flex; align-items: center; gap: 6px; }

.cell-line { display: flex; align-items: center; gap: 8px; }
.cell-title { font-size: 13.5px; font-weight: 550; color: var(--text); }
.ident { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--text-3); }
.perm-summary { font-size: 13px; color: var(--text-2); }

.row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.more { padding: 5px 8px; color: var(--danger); }

.empty-state { padding: 36px 0; }
.empty-title { font-size: 14px; font-weight: 600; color: var(--text-2); }
.empty-sub { font-size: 12.5px; color: var(--text-3); margin-top: 6px; }

.field-hint { font-size: 12px; color: var(--text-3); line-height: 1.6; margin-top: 4px; }

.perm-box {
  width: 100%;
  max-height: 46vh;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 6px 14px;
}
.perm-group { padding: 8px 0; }
.perm-group + .perm-group { border-top: 1px solid var(--border); }
.perm-group-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.perm-group-name { font-size: 12.5px; font-weight: 600; color: var(--text-2); }
.perm-group-count { font-size: 11.5px; color: var(--text-3); }
.perm-group-head .el-button { padding: 0; font-size: 12px; }

.perm-item { display: flex; width: 100%; height: auto; margin: 2px 0; }
.perm-item :deep(.el-checkbox__label) {
  display: flex;
  flex-direction: column;
  gap: 1px;
  white-space: normal;
  line-height: 1.45;
}
.perm-item-name { font-size: 13px; color: var(--text); }
.perm-item-desc { font-size: 12px; color: var(--text-3); }
</style>
