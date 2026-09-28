<template>
  <div class="page-shell">
    <header class="page-header">
      <div class="ph-main">
        <div class="ph-eyebrow">系统管理</div>
        <h1 class="page-title">群组管理</h1>
        <p class="page-desc">群组用于知识、笔记本等资源的可见域；同步来源的群组与成员由统一认证维护（SSO/LDAP 登录自动登记），仅本地成员可在本页调整。</p>
      </div>
      <div class="ph-actions">
        <div class="create-row">
          <el-input
            v-model="newName"
            class="new-name"
            maxlength="64"
            placeholder="新组名称"
            clearable
            @keyup.enter="createGroup"
          />
          <el-button
            type="primary"
            class="btn-new"
            :loading="isSaving('create')"
            :disabled="savingKeys.size > 0"
            @click="createGroup"
          >
            <el-icon><Plus /></el-icon><span>新建组</span>
          </el-button>
        </div>
      </div>
    </header>

    <div class="page-body">
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">组列表</div>
          <div class="panel-tools">
            <el-button :loading="loading" @click="load">刷新</el-button>
          </div>
        </div>

        <el-table v-loading="loading" :data="groups" row-key="id">
          <el-table-column label="组名" min-width="220">
            <template #default="{ row }">
              <span class="cell-title">{{ row.name }}</span>
            </template>
          </el-table-column>

          <el-table-column label="来源" width="100">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.source === 'local' ? 'info' : 'warning'">
                {{ row.source === 'local' ? '本地' : '同步' }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="成员数" width="100">
            <template #default="{ row }">
              <span class="cell-title">{{ row.member_count }}</span>
            </template>
          </el-table-column>

          <el-table-column label="引用数" width="110">
            <template #default="{ row }">
              <el-tooltip v-if="row.ref_count > 0" content="仍有资源引用该组，删除会被拒绝" placement="top">
                <span class="ref-warn">{{ row.ref_count }}</span>
              </el-tooltip>
              <span v-else class="muted">{{ row.ref_count }}</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="200" align="right">
            <template #default="{ row }">
              <div class="row-actions">
                <el-button size="small" :disabled="isRowBusy(row.id)" @click="openMembers(row)">成员管理</el-button>
                <el-button
                  size="small"
                  class="more"
                  :loading="isSaving('del:' + row.id)"
                  :disabled="isRowBusy(row.id)"
                  @click="removeGroup(row)"
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
                <div class="empty-sub">无法获取组列表，请稍后重试</div>
                <el-button size="small" style="margin-top: 12px" @click="load">重试</el-button>
              </template>
              <template v-else>
                <div class="empty-title">暂无组</div>
                <div class="empty-sub">在右上角输入组名，创建第一个组</div>
              </template>
            </div>
          </template>
        </el-table>
      </div>
    </div>

    <!-- 成员管理抽屉 -->
    <el-drawer v-model="drawerOpen" :title="drawerTitle" size="480px" @closed="onDrawerClosed">
      <div class="drawer-block">
        <div class="drawer-section-head">
          <span class="drawer-section-title">成员列表</span>
          <span class="drawer-section-count">{{ displayMembers.length }} 人</span>
        </div>
        <el-table v-loading="membersLoading" :data="displayMembers" size="small" row-key="id" max-height="320">
          <el-table-column label="用户" min-width="180">
            <template #default="{ row }">
              <div class="cell-line">
                <span class="cell-title">{{ row.display_name || row.username }}</span>
                <span v-if="row.display_name" class="username-sub">{{ row.username }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="来源" width="80">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.is_local ? 'info' : 'warning'">
                {{ row.is_local ? '本地' : '同步' }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="80" align="right">
            <template #default="{ row }">
              <el-tooltip content="由登录同步维护" :disabled="row.is_local" placement="top">
                <span class="tt">
                  <el-button
                    link
                    type="danger"
                    size="small"
                    :disabled="!row.is_local || membersSaving"
                    @click="stageRemove(row.id)"
                  >移除</el-button>
                </span>
              </el-tooltip>
            </template>
          </el-table-column>

          <template #empty>
            <div class="empty-state small">
              <template v-if="membersError">
                <div class="empty-title">成员加载失败</div>
                <div class="empty-sub">{{ membersErrorText || '请稍后重试' }}</div>
                <el-button size="small" style="margin-top: 12px" @click="reloadMembers">重试</el-button>
              </template>
              <template v-else>
                <div class="empty-title">暂无成员</div>
                <div class="empty-sub">从下方下拉框添加本地账号</div>
              </template>
            </div>
          </template>
        </el-table>
      </div>

      <div class="drawer-block">
        <div class="drawer-section-head">
          <span class="drawer-section-title">添加本地成员</span>
        </div>
        <el-select
          v-model="selectedIds"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="选择本地账号（可多选）"
          style="width: 100%"
          :loading="usersLoading"
        >
          <el-option v-for="u in memberOptions" :key="u.id" :label="userLabel(u)" :value="u.id" />
        </el-select>
        <div v-if="usersError" class="field-hint">
          本地用户加载失败{{ usersErrorText ? `：${usersErrorText}` : '' }}
          <el-button link size="small" @click="loadLocalUsers">重试</el-button>
        </div>
        <div v-else class="field-hint">仅本地账号可加入组；SSO/LDAP 成员的组由登录同步维护，此处只读。</div>
      </div>

      <template #footer>
        <el-button :disabled="membersSaving" @click="drawerOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="membersSaving"
          :disabled="membersSaving || !dirty"
          @click="saveMembers"
        >保存</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Trash2 } from 'lucide-vue-next'
import http from '../../api/http'
import { errText } from '../../utils/error'

interface GroupRow {
  id: string
  name: string
  source: string
  member_count: number
  ref_count: number
}
interface MemberRow {
  id: string
  username: string
  display_name: string
  is_local: boolean
}

const groups = ref<GroupRow[]>([])
const loading = ref(false)
const loadError = ref(false)
const loadedOnce = ref(false)
const savingKeys = ref<Set<string>>(new Set())
const newName = ref('')

const drawerOpen = ref(false)
const drawerGroup = ref<GroupRow | null>(null)
const members = ref<MemberRow[]>([])
const membersLoading = ref(false)
const membersError = ref(false)
const membersErrorText = ref('')
const selectedIds = ref<string[]>([])
const snapshot = ref<Set<string>>(new Set())

const localUsers = ref<MemberRow[]>([])
const usersLoaded = ref(false)
const usersLoading = ref(false)
const usersError = ref(false)
const usersErrorText = ref('')

let reqSeq = 0
let membersSeq = 0
let usersSeq = 0

const drawerTitle = computed(() => (drawerGroup.value ? `成员管理：${drawerGroup.value.name}` : '成员管理'))
const membersSaving = computed(() => (drawerGroup.value ? isSaving(`members:${drawerGroup.value.id}`) : false))

/** 下拉候选项:已加载的本地用户 + 当前组内本地成员(防止分页 100 之外的成员丢失标签) */
const memberOptions = computed<MemberRow[]>(() => {
  const map = new Map<string, MemberRow>()
  for (const u of localUsers.value) map.set(u.id, u)
  for (const m of members.value) if (m.is_local && !map.has(m.id)) map.set(m.id, m)
  return [...map.values()]
})

/** 展示用成员:同步成员恒显示;本地成员按当前暂存选择过滤(移除先暂存,保存后提交) */
const displayMembers = computed<MemberRow[]>(() => {
  const selected = new Set(selectedIds.value)
  return [
    ...members.value.filter(m => !m.is_local),
    ...memberOptions.value.filter(o => selected.has(o.id)),
  ].sort((a, b) => a.username.localeCompare(b.username))
})

/** 与打开抽屉/上次保存后的本地成员快照对比,有无暂存变更 */
const dirty = computed(() => {
  const selected = new Set(selectedIds.value)
  if (selected.size !== snapshot.value.size) return true
  for (const id of selected) if (!snapshot.value.has(id)) return true
  return false
})

/** 登记写操作键(create / del:<id> / members:<id>);同键重复触发直接忽略 */
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

function isSaving(key: string): boolean {
  return savingKeys.value.has(key)
}

/** 该行是否有写操作进行中:行内其余按钮一并禁用,但不影响其他行 */
function isRowBusy(id: string): boolean {
  return [...savingKeys.value].some(k => k.endsWith(`:${id}`))
}

function userLabel(u: MemberRow): string {
  return u.display_name ? `${u.display_name}(${u.username})` : u.username
}

async function load() {
  const seq = ++reqSeq
  loading.value = true
  try {
    const res = await http.get('/api/admin/groups')
    if (seq !== reqSeq) return
    groups.value = res.data.items || []
    loadedOnce.value = true
    loadError.value = false
  } catch (e: any) {
    if (seq !== reqSeq) return
    if (!loadedOnce.value) {
      groups.value = []
      loadError.value = true
    }
    ElMessage.error(errText(e, '加载失败'))
  } finally {
    if (seq === reqSeq) loading.value = false
  }
}

async function createGroup() {
  const name = newName.value.trim()
  if (!name) {
    ElMessage.warning('请填写组名')
    return
  }
  if (!beginSaving('create')) return
  try {
    await http.post('/api/admin/groups', { name })
    newName.value = ''
    ElMessage.success('已创建')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving('create')
  }
}

async function removeGroup(g: GroupRow) {
  try {
    await ElMessageBox.confirm(
      `删除组「${g.name}」？将同时移除其成员关系；有资源引用时会被拒绝`,
      '确认',
      { type: 'warning' }
    )
  } catch {
    return
  }
  const key = `del:${g.id}`
  if (!beginSaving(key)) return
  try {
    await http.delete(`/api/admin/groups/${g.id}`)
    ElMessage.success('已删除')
    await load()
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving(key)
  }
}

function openMembers(g: GroupRow) {
  drawerGroup.value = g
  members.value = []
  selectedIds.value = []
  snapshot.value = new Set()
  membersError.value = false
  membersErrorText.value = ''
  drawerOpen.value = true
  loadMembers(g.id)
  if (!usersLoaded.value && !usersLoading.value) loadLocalUsers()
}

async function loadMembers(groupId: string) {
  const seq = ++membersSeq
  membersLoading.value = true
  try {
    const res = await http.get(`/api/admin/groups/${groupId}/members`)
    if (seq !== membersSeq) return
    const items: MemberRow[] = res.data.items || []
    members.value = items
    const localIds = items.filter(m => m.is_local).map(m => m.id)
    selectedIds.value = localIds
    snapshot.value = new Set(localIds)
    membersError.value = false
    membersErrorText.value = ''
  } catch (e: any) {
    if (seq !== membersSeq) return
    membersError.value = true
    membersErrorText.value = errText(e, '请稍后重试')
  } finally {
    if (seq === membersSeq) membersLoading.value = false
  }
}

function reloadMembers() {
  if (drawerGroup.value) loadMembers(drawerGroup.value.id)
}

async function loadLocalUsers() {
  const seq = ++usersSeq
  usersLoading.value = true
  usersError.value = false
  try {
    const res = await http.get('/api/admin/users', { params: { page_size: 100 } })
    if (seq !== usersSeq) return
    localUsers.value = (res.data.items || []).filter((u: MemberRow) => u.is_local)
    usersLoaded.value = true
    usersErrorText.value = ''
  } catch (e: any) {
    if (seq !== usersSeq) return
    usersError.value = true
    usersErrorText.value = errText(e, '请稍后重试')
  } finally {
    if (seq === usersSeq) usersLoading.value = false
  }
}

function stageRemove(id: string) {
  selectedIds.value = selectedIds.value.filter(x => x !== id)
}

async function saveMembers() {
  const g = drawerGroup.value
  if (!g) return
  const add = selectedIds.value.filter(id => !snapshot.value.has(id))
  const remove = [...snapshot.value].filter(id => !selectedIds.value.includes(id))
  if (!add.length && !remove.length) {
    ElMessage.info('成员未变更')
    return
  }
  const key = `members:${g.id}`
  if (!beginSaving(key)) return
  try {
    await http.put(`/api/admin/groups/${g.id}/members`, { add, remove })
    ElMessage.success('成员已更新')
    await Promise.all([loadMembers(g.id), load()])
  } catch (e: any) {
    ElMessage.error(errText(e))
  } finally {
    endSaving(key)
  }
}

function onDrawerClosed() {
  membersSeq++
  drawerGroup.value = null
  members.value = []
  selectedIds.value = []
  snapshot.value = new Set()
  membersError.value = false
  membersErrorText.value = ''
}

onMounted(load)
</script>

<style scoped>
.btn-new { display: inline-flex; align-items: center; gap: 6px; }
.create-row { display: flex; align-items: center; gap: 10px; }
.new-name { width: 220px; }
.new-name :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--border) inset; }

.cell-line { display: flex; align-items: center; gap: 8px; }
.cell-title { font-size: 13.5px; font-weight: 550; color: var(--text); }
.username-sub { font-size: 12px; color: var(--text-3); }
.muted { color: var(--text-3); font-size: 13px; }
.ref-warn { color: var(--warning); font-weight: 600; cursor: help; }

.row-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
.more { padding: 5px 8px; color: var(--danger); }
.tt { display: inline-flex; }

.empty-state { padding: 36px 0; }
.empty-state.small { padding: 16px 0; }
.empty-title { font-size: 14px; font-weight: 600; color: var(--text-2); }
.empty-sub { font-size: 12.5px; color: var(--text-3); margin-top: 6px; }

.drawer-block { margin-bottom: 22px; }
.drawer-section-head { display: flex; align-items: baseline; justify-content: space-between; margin: 0 0 10px; }
.drawer-section-title { font-size: 13.5px; font-weight: 600; color: var(--text); }
.drawer-section-count { font-size: 12px; color: var(--text-3); }

.field-hint { font-size: 12px; color: var(--text-3); line-height: 1.6; margin-top: 4px; }
.field-hint .el-button { padding: 0; font-size: 12px; }
</style>
