<template>
  <div class="page-shell">
    <header class="page-header">
      <div class="ph-main">
        <div class="ph-eyebrow">系统管理</div>
        <h1 class="page-title">用户管理</h1>
        <p class="page-desc">本地账号可新建、禁用、重置密码并调整组；SSO/LDAP 用户的资料与组由统一认证同步维护，仅可分配角色。</p>
      </div>
      <div class="ph-actions">
        <el-button type="primary" class="btn-new" @click="openCreate">
          <el-icon><Plus /></el-icon><span>新建本地账号</span>
        </el-button>
      </div>
    </header>

    <div class="page-body">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">用户列表</div>
          <div class="panel-tools">
            <el-input
              v-model="searchInput"
              placeholder="搜索用户名 / 姓名 / 邮箱"
              clearable
              class="search"
              @keyup.enter="search"
              @clear="search"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-button :disabled="loading" @click="search">搜索</el-button>
            <el-button :loading="loading" @click="load">刷新</el-button>
          </div>
        </div>

        <el-table v-loading="loading" :data="users" row-key="id">
          <el-table-column label="用户名" min-width="150">
            <template #default="{ row }">
              <div class="cell-line">
                <span class="cell-title">{{ row.username }}</span>
                <el-tag v-if="row.id === myId" size="small" effect="plain" type="info">我</el-tag>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="姓名" min-width="130">
            <template #default="{ row }">{{ row.name || '—' }}</template>
          </el-table-column>

          <el-table-column label="邮箱" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ row.email || '—' }}</template>
          </el-table-column>

          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.is_local ? 'info' : 'warning'">
                {{ row.is_local ? '本地' : 'SSO' }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.is_active ? 'success' : 'danger'">
                {{ row.is_active ? '启用' : '禁用' }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="角色" min-width="200">
            <template #default="{ row }">
              <div class="tag-wrap">
                <el-tag v-for="r in row.roles" :key="r.name" size="small" effect="plain" class="tag">
                  {{ r.display_name }}
                </el-tag>
                <el-tooltip
                  v-if="row.is_marked_admin"
                  content="由系统标记/认证来源决定，不随角色分配移除"
                  placement="top"
                >
                  <el-tag size="small" effect="plain" type="warning" class="tag">内部管理员</el-tag>
                </el-tooltip>
                <span v-if="!row.roles.length && !row.is_marked_admin" class="muted">—</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="组" min-width="180">
            <template #default="{ row }">
              <el-tooltip :content="row.is_local ? '' : '由登录同步维护'" :disabled="row.is_local" placement="top">
                <div class="tag-wrap">
                  <el-tag v-for="g in visibleGroups(row)" :key="g" size="small" effect="plain" class="tag">
                    {{ g }}
                  </el-tag>
                  <span v-if="!visibleGroups(row).length" class="muted">—</span>
                </div>
              </el-tooltip>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="330" align="right">
            <template #default="{ row }">
              <div class="row-actions">
                <el-tooltip content="资料由统一认证同步维护" :disabled="row.is_local" placement="top">
                  <span class="tt">
                    <el-button size="small" :disabled="!row.is_local || isRowBusy(row.id)" @click="openEdit(row)">
                      编辑
                    </el-button>
                  </span>
                </el-tooltip>
                <el-button
                  size="small"
                  :loading="isSaving('toggle:' + row.id)"
                  :disabled="isRowBusy(row.id)"
                  @click="toggleActive(row)"
                >
                  {{ row.is_active ? '禁用' : '启用' }}
                </el-button>
                <el-tooltip content="仅本地账号可重置密码" :disabled="row.is_local" placement="top">
                  <span class="tt">
                    <el-button
                      size="small"
                      :loading="isSaving('pwd:' + row.id)"
                      :disabled="!row.is_local || isRowBusy(row.id)"
                      @click="resetPassword(row)"
                    >
                      重置密码
                    </el-button>
                  </span>
                </el-tooltip>
                <el-button size="small" :disabled="isRowBusy(row.id)" @click="openRoles(row)">角色</el-button>
                <el-tooltip content="由登录同步维护" :disabled="row.is_local" placement="top">
                  <span class="tt">
                    <el-button size="small" :disabled="!row.is_local || isRowBusy(row.id)" @click="openGroups(row)">
                      组
                    </el-button>
                  </span>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>

          <template #empty>
            <div class="empty-state">
              <template v-if="loadError">
                <div class="empty-title">加载失败</div>
                <div class="empty-sub">无法获取用户列表，请稍后重试</div>
                <el-button size="small" style="margin-top: 12px" @click="load">重试</el-button>
              </template>
              <template v-else>
                <div class="empty-title">{{ activeQuery ? '未找到匹配用户' : '暂无用户' }}</div>
                <div class="empty-sub">
                  {{ activeQuery ? '换个关键词试试' : '点击右上角「新建本地账号」创建第一个用户' }}
                </div>
              </template>
            </div>
          </template>
        </el-table>

        <div v-if="total > 0" class="pager-row">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[10, 20, 50, 100]"
            layout="total, sizes, prev, pager, next, jumper"
            background
            @change="load"
          />
        </div>
      </div>
    </div>

    <!-- 新建本地账号 -->
    <el-dialog v-model="createOpen" title="新建本地账号" width="560px" top="6vh">
      <el-form label-width="90px">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" maxlength="64" placeholder="登录用户名，创建后不可修改" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="createForm.password" type="password" show-password maxlength="128" placeholder="初始密码" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="createForm.name" maxlength="64" placeholder="可选" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="createForm.email" maxlength="128" placeholder="可选" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select
            v-model="createForm.roles"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择角色（可多选）"
            style="width: 100%"
          >
            <el-option v-for="r in roleOptions" :key="r.name" :label="r.display_name" :value="r.name" />
          </el-select>
          <div v-if="rolesError" class="field-hint">
            加载失败，请检查网络后重试
            <el-button link size="small" @click="loadOptions">重试</el-button>
          </div>
        </el-form-item>
        <el-form-item label="组">
          <el-select
            v-model="createForm.groups"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择组（可多选）"
            style="width: 100%"
          >
            <el-option v-for="g in groupOptions" :key="g.name" :label="g.name" :value="g.name" />
          </el-select>
          <div v-if="groupsError" class="field-hint">
            加载失败，请检查网络后重试
            <el-button link size="small" @click="loadOptions">重试</el-button>
          </div>
          <div v-else class="field-hint">组用于控制资源可见范围；SSO/LDAP 用户的组由登录同步维护，本地账号可自由调整。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="isSaving('create')"
          :disabled="savingKeys.size > 0"
          @click="saveCreate"
        >创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑资料 -->
    <el-dialog v-model="editOpen" title="编辑资料" width="480px">
      <el-form label-width="90px">
        <el-form-item label="用户名">
          <el-input :model-value="editTarget?.username || ''" disabled />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="editForm.name" maxlength="64" placeholder="可选" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="editForm.email" maxlength="128" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="isSaving('edit:' + (editTarget?.id || ''))"
          :disabled="savingKeys.size > 0"
          @click="saveEdit"
        >保存</el-button>
      </template>
    </el-dialog>

    <!-- 分配角色 -->
    <el-dialog v-model="rolesOpen" title="分配角色" width="520px">
      <div class="dialog-target">用户：{{ targetLabel(rolesTarget) }}</div>
      <el-select
        v-model="rolesForm"
        multiple
        filterable
        collapse-tags
        collapse-tags-tooltip
        placeholder="选择角色（可多选）"
        style="width: 100%"
      >
        <el-option v-for="r in roleOptions" :key="r.name" :label="r.display_name" :value="r.name" />
      </el-select>
      <div v-if="rolesError" class="field-hint">
        加载失败，请检查网络后重试
        <el-button link size="small" @click="loadOptions">重试</el-button>
      </div>
      <div v-else class="field-hint">保存后按所选角色全量替换，留空表示不赋予任何角色。</div>
      <div class="field-hint">内部管理员标记由系统标记/认证来源决定，不随角色分配移除。</div>
      <template #footer>
        <el-button @click="rolesOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="isSaving('roles:' + (rolesTarget?.id || ''))"
          :disabled="savingKeys.size > 0"
          @click="saveRoles"
        >保存</el-button>
      </template>
    </el-dialog>

    <!-- 分配组（仅本地账号） -->
    <el-dialog v-model="groupsOpen" title="分配组" width="520px">
      <div class="dialog-target">用户：{{ targetLabel(groupsTarget) }}</div>
      <el-select
        v-model="groupsForm"
        multiple
        filterable
        collapse-tags
        collapse-tags-tooltip
        placeholder="选择组（可多选）"
        style="width: 100%"
      >
        <el-option v-for="g in groupOptions" :key="g.name" :label="g.name" :value="g.name" />
      </el-select>
      <div v-if="groupsError" class="field-hint">
        加载失败，请检查网络后重试
        <el-button link size="small" @click="loadOptions">重试</el-button>
      </div>
      <div v-else class="field-hint">保存后按所选组全量替换（内部管理员标记不受影响）。</div>
      <template #footer>
        <el-button @click="groupsOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="isSaving('groups:' + (groupsTarget?.id || ''))"
          :disabled="savingKeys.size > 0"
          @click="saveGroups"
        >保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from 'lucide-vue-next'
import http from '../../api/http'
import { useAuthStore } from '../../stores/auth'
import { errText } from '../../utils/error'

interface RoleBrief {
  name: string
  display_name: string
}
interface UserRow {
  id: string
  username: string
  email: string
  name: string
  is_local: boolean
  is_active: boolean
  roles: RoleBrief[]
  groups: string[]
  is_marked_admin: boolean
}
interface RoleOption {
  name: string
  display_name: string
}
interface GroupOption {
  name: string
}

const auth = useAuthStore()
const myId = computed(() => auth.user?.id || '')

const users = ref<UserRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const searchInput = ref('')
const activeQuery = ref('')
const loading = ref(false)
const loadError = ref(false)
const loadedOnce = ref(false)
const savingKeys = ref<Set<string>>(new Set())

const roleOptions = ref<RoleOption[]>([])
const groupOptions = ref<GroupOption[]>([])
const rolesError = ref(false)
const groupsError = ref(false)

const createOpen = ref(false)
const createForm = reactive({
  username: '',
  password: '',
  name: '',
  email: '',
  roles: [] as string[],
  groups: [] as string[],
})

const editOpen = ref(false)
const editTarget = ref<UserRow | null>(null)
const editForm = reactive({ name: '', email: '' })

const rolesOpen = ref(false)
const rolesTarget = ref<UserRow | null>(null)
const rolesForm = ref<string[]>([])

const groupsOpen = ref(false)
const groupsTarget = ref<UserRow | null>(null)
const groupsForm = ref<string[]>([])

let reqSeq = 0
let optionsSeq = 0

/** 登记写操作键(create / edit:<id> / toggle:<id> / pwd:<id> / roles:<id> / groups:<id>);同键重复触发直接忽略 */
function beginSaving(key: string): boolean {
  if (savingKeys.value.has(key)) return false
  savingKeys.value = new Set(savingKeys.value).add(key)
  return true
}

function endSaving(key: string) {
  const next = new Set(savingKeys.value)
  next.delete(key)
  savingKeys.value = next
}

/** 某写操作是否进行中 */
function isSaving(key: string): boolean {
  return savingKeys.value.has(key)
}

/** 该行是否有写操作进行中:行内其余按钮一并禁用,但不影响其他行(create 无 id) */
function isRowBusy(id: string): boolean {
  return [...savingKeys.value].some(k => k.endsWith(`:${id}`))
}

function visibleGroups(u: UserRow): string[] {
  return u.groups.filter(g => !g.startsWith('__'))
}

function targetLabel(u: UserRow | null): string {
  if (!u) return ''
  return u.name ? `${u.name}（${u.username}）` : u.username
}

async function load() {
  const seq = ++reqSeq
  loading.value = true
  try {
    const res = await http.get('/api/admin/users', {
      params: { query: activeQuery.value, page: page.value, page_size: pageSize.value },
    })
    if (seq !== reqSeq) return
    users.value = res.data.items || []
    total.value = res.data.total || 0
    loadedOnce.value = true
    loadError.value = false
  } catch (e: any) {
    if (seq !== reqSeq) return
    if (!loadedOnce.value) {
      users.value = []
      total.value = 0
      loadError.value = true
    }
    ElMessage.error(errText(e, '加载失败'))
  } finally {
    if (seq === reqSeq) loading.value = false
  }
}

function search() {
  activeQuery.value = searchInput.value.trim()
  page.value = 1
  load()
}

async function loadOptions() {
  const seq = ++optionsSeq
  rolesError.value = false
  groupsError.value = false
  const [r, g] = await Promise.allSettled([
    http.get('/api/admin/roles'),
    http.get('/api/admin/groups'),
  ])
  if (seq !== optionsSeq) return
  if (r.status === 'fulfilled') {
    roleOptions.value = r.value.data.items || []
  } else {
    rolesError.value = true
  }
  if (g.status === 'fulfilled') {
    groupOptions.value = g.value.data.items || []
  } else {
    groupsError.value = true
  }
}

function openCreate() {
  Object.assign(createForm, {
    username: '',
    password: '',
    name: '',
    email: '',
    roles: [],
    groups: [],
  })
  createOpen.value = true
}

async function saveCreate() {
  const username = createForm.username.trim()
  if (!username) {
    ElMessage.warning('请填写用户名')
    return
  }
  if (!createForm.password.trim()) {
    ElMessage.warning('请填写密码')
    return
  }
  if (!beginSaving('create')) return
  try {
    await http.post('/api/admin/users', {
      username,
      password: createForm.password,
      name: createForm.name.trim(),
      email: createForm.email.trim(),
      roles: createForm.roles,
      groups: createForm.groups,
    })
    createOpen.value = false
    ElMessage.success('已创建')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving('create')
  }
}

function openEdit(u: UserRow) {
  if (!u.is_local) return
  editTarget.value = u
  editForm.name = u.name
  editForm.email = u.email
  editOpen.value = true
}

async function saveEdit() {
  if (!editTarget.value) return
  const key = `edit:${editTarget.value.id}`
  if (!beginSaving(key)) return
  try {
    await http.put(`/api/admin/users/${editTarget.value.id}`, {
      name: editForm.name.trim(),
      email: editForm.email.trim(),
    })
    editOpen.value = false
    ElMessage.success('已保存')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving(key)
  }
}

async function toggleActive(u: UserRow) {
  if (u.is_active) {
    try {
      await ElMessageBox.confirm(
        `确认禁用用户「${u.username}」？禁用后该用户将无法登录，已登录会话也会失效。`,
        '确认',
        { type: 'warning' }
      )
    } catch {
      return
    }
  }
  const key = `toggle:${u.id}`
  if (!beginSaving(key)) return
  try {
    await http.put(`/api/admin/users/${u.id}`, { is_active: !u.is_active })
    ElMessage.success(u.is_active ? '已禁用' : '已启用')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving(key)
  }
}

async function resetPassword(u: UserRow) {
  let password = ''
  try {
    const res = await ElMessageBox.prompt(`为用户「${u.username}」设置新密码`, '重置密码', {
      inputType: 'password',
      inputPlaceholder: '请输入新密码',
      confirmButtonText: '重置',
      cancelButtonText: '取消',
      inputValidator: (v: string) => (v && v.trim() ? true : '密码不能为空'),
    })
    password = res.value
  } catch {
    return
  }
  const key = `pwd:${u.id}`
  if (!beginSaving(key)) return
  try {
    await http.post(`/api/admin/users/${u.id}/password`, { password })
    ElMessage.success('密码已重置')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving(key)
  }
}

function openRoles(u: UserRow) {
  rolesTarget.value = u
  rolesForm.value = u.roles.map(r => r.name)
  rolesOpen.value = true
}

async function saveRoles() {
  if (!rolesTarget.value) return
  const key = `roles:${rolesTarget.value.id}`
  if (!beginSaving(key)) return
  try {
    await http.put(`/api/admin/users/${rolesTarget.value.id}/roles`, { roles: rolesForm.value })
    rolesOpen.value = false
    ElMessage.success('角色已更新')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving(key)
  }
}

function openGroups(u: UserRow) {
  if (!u.is_local) return
  groupsTarget.value = u
  groupsForm.value = visibleGroups(u)
  groupsOpen.value = true
}

async function saveGroups() {
  if (!groupsTarget.value) return
  const key = `groups:${groupsTarget.value.id}`
  if (!beginSaving(key)) return
  try {
    await http.put(`/api/admin/users/${groupsTarget.value.id}/groups`, { groups: groupsForm.value })
    groupsOpen.value = false
    ElMessage.success('组已更新')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving(key)
  }
}

onMounted(() => {
  load()
  loadOptions()
})
</script>

<style scoped>
.btn-new { display: inline-flex; align-items: center; gap: 6px; }
.search { width: 250px; }
.search :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--border) inset; }

.cell-line { display: flex; align-items: center; gap: 8px; }
.cell-title { font-size: 13.5px; font-weight: 550; color: var(--text); }
.muted { color: var(--text-3); font-size: 13px; }

.tag-wrap { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.tag { margin: 0; }

.row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
.tt { display: inline-flex; }

.pager-row { display: flex; justify-content: flex-end; padding: 14px 0 2px; }

.empty-state { padding: 36px 0; }
.empty-title { font-size: 14px; font-weight: 600; color: var(--text-2); }
.empty-sub { font-size: 12.5px; color: var(--text-3); margin-top: 6px; }

.field-hint { font-size: 12px; color: var(--text-3); line-height: 1.6; margin-top: 4px; }
.field-hint .el-button { padding: 0; font-size: 12px; }
.dialog-target { font-size: 13px; color: var(--text-2); margin-bottom: 12px; }
</style>
