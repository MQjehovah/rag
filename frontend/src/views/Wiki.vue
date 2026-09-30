<template>
  <div class="wiki-page">
    <aside class="wiki-sidebar">
      <div class="wiki-sidebar-header">
        <span class="wiki-brand">知识库 Wiki</span>
      </div>
      <div class="space-bar">
        <div v-if="isAdmin" class="space-item" :class="{ active: activeSpace === '' }" @click="activeSpace = ''">
          <span class="space-icon">📚</span>
          <span class="space-name">全部</span>
          <span class="space-count">{{ totalAll }}</span>
          <el-dropdown v-if="isAdmin" trigger="click" @command="(cmd: string) => { if (cmd === 'create') spaceDialog = true }">
            <button class="space-menu" title="空间操作" @click.stop>⋯</button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="create">新建空间</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <div class="space-item" :class="{ active: activeSpace === 'default' }" @click="activeSpace = 'default'">
          <span class="space-icon">📄</span>
          <span class="space-name">默认空间</span>
          <span class="space-count">{{ defaultCount }}</span>
          <el-dropdown v-if="isAdmin" trigger="click" @command="(cmd: string) => { if (cmd === 'edit' && defaultSpaceMeta) openSpaceSettings({ ...defaultSpaceMeta, is_default: true }) }">
            <button class="space-menu" title="空间设置" @click.stop>⋯</button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="edit">编辑空间</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <div
          v-for="s in spaces"
          :key="s.id"
          class="space-item"
          :class="{ active: activeSpace === s.id }"
          @click="activeSpace = s.id"
        >
          <span class="space-icon">{{ s.icon || '🗂️' }}</span>
          <span class="space-name">{{ s.name }}</span>
          <span class="space-count">{{ s.count }}</span>
          <el-dropdown
            v-if="isAdmin"
            trigger="click"
            @command="(cmd: string) => { if (cmd === 'edit') openSpaceSettings(s) }"
          >
            <button class="space-menu" title="空间设置" @click.stop>⋯</button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="edit">编辑空间</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <button v-if="isAdmin" class="space-add" @click="spaceDialog = true">＋ 新建空间</button>
      </div>
      <el-input
        v-model="filterText"
        placeholder="搜索页面..."
        clearable
        size="small"
        class="wiki-filter"
      />
      <div class="wiki-cat-list">
        <div class="wiki-tree">
          <div
            v-for="row in wikiTreeRows"
            :key="row.page.id"
            class="wiki-tree-item"
            :class="{
              active: current && current.id === row.page.id,
              dragging: wikiDragId === row.page.id,
              'drop-before': wikiDrop?.id === row.page.id && wikiDrop?.zone === 'before',
              'drop-after': wikiDrop?.id === row.page.id && wikiDrop?.zone === 'after',
              'drop-inside': wikiDrop?.id === row.page.id && wikiDrop?.zone === 'inside',
            }"
            :style="{ paddingLeft: (8 + row.depth * 14) + 'px' }"
            :draggable="isAdmin"
            @dragstart="onWikiDragStart(row.page, $event)"
            @dragover="onWikiDragOver(row.page, $event)"
            @drop.stop="onWikiDrop(row.page, $event)"
            @dragend="onWikiDragEnd"
            @click="openPage(row.page.id)"
          >
            <span
              v-if="row.hasChildren"
              class="wiki-tree-chev"
              :class="{ open: !row.collapsed }"
              @click.stop="toggleWiki(row.page.id)"
            >›</span>
            <span v-else class="wiki-tree-chev placeholder"></span>
            <span class="wiki-tree-title">{{ row.page.title }}</span>
            <el-dropdown v-if="isAdmin" trigger="click" @command="(c: string) => c === 'child' ? createWikiPage(row.page.id) : deleteWikiPage(row.page.id)">
              <button class="wiki-tree-menu" title="更多" @click.stop>⋯</button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="child">新建子页面</el-dropdown-item>
                  <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </div>
        <div v-if="isAdmin" class="wiki-tree-add" @click="createWikiPage(null)">＋ 新建页面</div>
        <el-empty
          v-if="!total && !running"
          description="Wiki 尚未生成(编译任务完成后自动生成)"
          :image-size="60"
        />
      </div>
    </aside>
    <main class="wiki-main">
      <div v-if="current" class="wiki-content-card">
        <div class="wiki-card-actions">
          <template v-if="!editing && isAdmin">
            <el-dropdown trigger="click" @command="(sid: string) => moveToSpace(sid === 'default' ? null : sid)">
              <el-button size="small" text>移动到空间 ▾</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="default">📄 默认空间</el-dropdown-item>
                  <el-dropdown-item v-for="s in spaces" :key="s.id" :command="s.id">{{ s.icon || '🗂️' }} {{ s.name }}</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button size="small" text type="danger" @click="deleteWikiPage(current.id)">删除</el-button>
            <el-button size="small" text type="primary" @click="startEdit">编辑</el-button>
          </template>
          <span v-if="editing" class="wiki-edit-tip">编辑中（下次编译会保留你的修改）</span>
        </div>
        <div class="wiki-crumb">{{ current.category }}</div>
        <h1 class="wiki-title">{{ current.title }}</h1>
        <div v-if="current.summary" class="wiki-summary">{{ current.summary }}</div>
        <div v-if="isAdmin" class="wiki-group-bar">
          <span class="wiki-group-label">归属组：{{ current.group_id || '公共' }}</span>
          <el-input
            v-model="groupInput"
            size="small"
            placeholder="输入组名，留空表示公共"
            class="wiki-group-input"
          />
          <el-button size="small" type="primary" :loading="savingGroup" @click="saveGroup">保存归属</el-button>
        </div>
        <template v-if="!editing">
          <div
            class="wiki-body markdown-body"
            ref="bodyRef"
            v-html="renderContent(current.content)"
            @click="handleContentClick"
            @mouseover="onBodyMouseOver"
            @mouseleave="hidePreview"
            @error.capture="handleImgError"
          ></div>
        </template>
        <template v-else>
          <div class="wiki-edit">
            <el-input v-model="editForm.category" placeholder="分类" size="small" class="wiki-edit-field" />
            <el-input v-model="editForm.summary" placeholder="摘要" size="small" class="wiki-edit-field" />
            <el-input
              v-model="editForm.content"
              type="textarea"
              :rows="20"
              placeholder="Markdown 正文，页面间引用用 [[页面标题]]"
              class="wiki-edit-content"
            />
            <div class="wiki-edit-actions">
              <el-button size="small" @click="editing = false">取消</el-button>
              <el-button size="small" type="primary" :loading="savingEdit" @click="saveEdit">保存</el-button>
            </div>
          </div>
        </template>
        <div v-if="current.sources && current.sources.length" class="wiki-sources">
          <span class="wiki-sources-label">来源笔记：</span>
          <router-link
            v-for="s in current.sources"
            :key="s.id"
            :to="{ path: '/notes', query: { page: s.id } }"
            class="wiki-source-link"
          >{{ s.title }}</router-link>
        </div>
      </div>
      <div v-else class="wiki-empty">
        <h2>{{ running ? 'Wiki 正在编译，请稍候...' : '选择左侧页面查看' }}</h2>
        <p v-if="running" class="wiki-progress">{{ progressText }}</p>
      </div>
    </main>

    <!-- 新建空间 -->
    <el-dialog v-model="spaceDialog" title="新建空间" width="440px">
      <el-form label-width="80px">
        <el-form-item label="图标">
          <el-input v-model="newSpace.icon" maxlength="4" style="width: 100px" />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="newSpace.name" placeholder="如：研发知识库" @keyup.enter="createSpace" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="newSpace.description" placeholder="可选" />
        </el-form-item>
        <el-form-item label="可见性">
          <el-select v-model="newSpace.visibility" style="width: 100%">
            <el-option label="仅本人可见" value="self" />
            <el-option label="部门可见" value="dept" />
            <el-option label="公开（所有登录用户）" value="public" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="spaceDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingSpace" @click="createSpace">创建</el-button>
      </template>
    </el-dialog>

    <!-- 空间设置 -->
    <el-dialog v-model="spaceEditDialog" title="空间设置" width="440px">
      <el-form label-width="80px">
        <template v-if="!editSpaceForm.isDefault">
          <el-form-item label="图标">
            <el-input v-model="editSpaceForm.icon" maxlength="4" style="width: 100px" />
          </el-form-item>
          <el-form-item label="名称">
            <el-input v-model="editSpaceForm.name" @keyup.enter="saveSpaceSettings" />
          </el-form-item>
          <el-form-item label="说明">
            <el-input v-model="editSpaceForm.description" placeholder="可选" />
          </el-form-item>
        </template>
        <el-form-item label="可见性">
          <el-select v-model="editSpaceForm.visibility" style="width: 100%">
            <el-option label="仅本人可见" value="self" />
            <el-option label="部门可见" value="dept" />
            <el-option label="公开（所有登录用户）" value="public" />
          </el-select>
        </el-form-item>
        <template v-if="isAdmin">
          <el-form-item label="可访问用户">
            <el-select
              v-model="editSpaceForm.acl_users"
              multiple
              filterable
              placeholder="额外授权具体用户"
              style="width: 100%"
            >
              <el-option
                v-for="u in aclUserOptions"
                :key="u.id"
                :label="`${u.name || u.username}（${u.username}）`"
                :value="u.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="可访问部门">
            <el-select
              v-model="editSpaceForm.acl_groups"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="额外授权部门/组"
              style="width: 100%"
            >
              <el-option v-for="g in aclGroupOptions" :key="g" :label="g" :value="g" />
            </el-select>
            <div class="muted-hint">在「仅本人 / 部门 / 公开」之外，额外授权所选用户与部门可访问。</div>
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="spaceEditDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingSpaceEdit" @click="saveSpaceSettings">保存</el-button>
      </template>
    </el-dialog>

    <!-- wiki-link 悬浮预览(页面标题 + 摘要/锚点段落) -->
    <el-popover
      :visible="preview.visible"
      :virtual-ref="previewRef"
      virtual-triggering
      placement="top"
      :width="340"
      :show-arrow="true"
    >
      <div class="wiki-preview">
        <div class="wiki-preview-title">{{ preview.title }}</div>
        <div class="wiki-preview-snippet">{{ preview.snippet }}</div>
      </div>
    </el-popover>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick, reactive, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import http from '../api/http'
import { signImageElement } from '../utils/imageSign'
import { createLazyObserver, observeLazyImages, type LazyObserver } from '../utils/lazyRender'
import {
  extractHeadingSection, markdownHeadingAnchors, markdownSnippet, splitWikiTarget,
} from '../utils/wikiAnchors'
import { useAuthStore } from '../stores/auth'
import { PERM } from '../constants/perms'
import MarkdownIt from 'markdown-it'
import taskLists from 'markdown-it-task-lists'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import mermaid from 'mermaid'

mermaid.initialize({ startOnLoad: false, theme: 'default' })

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true,
  highlight(str: string, lang: string) {
    if (lang && hljs.getLanguage(lang)) {
      try { return (hljs.highlight(str, { language: lang }) as any).value } catch {}
    }
    return (hljs.highlightAuto(str) as any).value
  },
}).use(taskLists, { enabled: false, label: true })
  .use(markdownHeadingAnchors)

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

interface WikiPageListItem {
  id: string
  title: string
  summary: string
}

type Visibility = 'self' | 'dept' | 'public'

const categories = ref<{ name: string; pages: WikiPageListItem[] }[]>([])
const spaces = ref<{ id: string; name: string; icon: string; description: string; count: number; visibility?: Visibility; owner_id?: string | null; acl_users?: string[]; acl_groups?: string[] }[]>([])
const activeSpace = ref<string>('')   // '' 全部 | 'default' 默认空间 | 空间 id
const defaultCount = ref(0)
/** 默认空间元数据(后端随 spaces 返回; 供默认空间的编辑入口预填 可见性/ACL) */
const defaultSpaceMeta = ref<{ id: string; name: string; icon: string; description: string; visibility?: Visibility; acl_users?: string[]; acl_groups?: string[] } | null>(null)
const spaceDialog = ref(false)
const newSpace = ref<{ name: string; icon: string; description: string; visibility: Visibility }>({ name: '', icon: '🗂️', description: '', visibility: 'dept' })
const savingSpace = ref(false)
const spaceEditDialog = ref(false)
const savingSpaceEdit = ref(false)
const editSpaceForm = ref<{ id: string; name: string; icon: string; description: string; visibility: Visibility; acl_users: string[]; acl_groups: string[]; isDefault: boolean }>({ id: '', name: '', icon: '', description: '', visibility: 'dept', acl_users: [], acl_groups: [], isDefault: false })
const aclUserOptions = ref<{ id: string; username: string; name: string }[]>([])
const aclGroupOptions = ref<string[]>([])
const total = ref(0)
const running = ref(false)
const current = ref<any>(null)
const filterText = ref('')

const editing = ref(false)
const savingEdit = ref(false)
const editForm = ref({ content: '', summary: '', category: '' })
const groupInput = ref('')
const savingGroup = ref(false)
const bodyRef = ref<HTMLElement>()

const isAdmin = computed(() => authStore.hasPerm(PERM.wiki))

const titleToId = computed(() => {
  const map: Record<string, string> = {}
  for (const cat of categories.value) {
    for (const p of cat.pages) map[p.title] = p.id
  }
  return map
})



const progressText = computed(() => {
  return status.value.total > 0 ? `${status.value.processed}/${status.value.total} 篇已蒸馏` : status.value.message
})

const status = ref<any>({ running: false, processed: 0, total: 0, message: '' })
let pollTimer: number | null = null

const loadList = async () => {
  try {
    const params: Record<string, string> = {}
    if (activeSpace.value) params.space_id = activeSpace.value
    const res = await http.get('/api/wiki', { params })
    items.value = res.data.items || []
    categories.value = res.data.categories || []
    total.value = res.data.total || 0
    running.value = !!res.data.running
  } catch { /* ignore */ }
}

const loadSpaces = async () => {
  try {
    const res = await http.get('/api/wiki/spaces')
    spaces.value = res.data.spaces || []
    defaultCount.value = res.data.default_count || 0
    defaultSpaceMeta.value = res.data.default_space || null
    // 「全部」仅管理员可见: 普通用户默认落在「默认空间」, 不停留在跨空间聚合视图
    if (!isAdmin.value && activeSpace.value === '') activeSpace.value = 'default'
  } catch { /* ignore */ }
}

const createSpace = async () => {
  if (!newSpace.value.name.trim()) { ElMessage.warning('请输入空间名称'); return }
  savingSpace.value = true
  try {
    await http.post('/api/wiki/spaces', { ...newSpace.value })
    spaceDialog.value = false
    newSpace.value = { name: '', icon: '🗂️', description: '', visibility: 'dept' }
    ElMessage.success('已创建空间')
    await loadSpaces()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '创建失败')
  } finally {
    savingSpace.value = false
  }
}

const openSpaceSettings = (s: { id: string; name: string; icon?: string; description?: string; visibility?: Visibility; acl_users?: string[]; acl_groups?: string[]; is_default?: boolean }) => {
  editSpaceForm.value = {
    id: s.id,
    name: s.name,
    icon: s.icon || '',
    description: s.description || '',
    visibility: (s.visibility === 'self' || s.visibility === 'public') ? s.visibility : 'dept',
    acl_users: s.acl_users || [],
    acl_groups: s.acl_groups || [],
    isDefault: !!s.is_default,
  }
  spaceEditDialog.value = true
  void loadAclOptions()
}

const loadAclOptions = async () => {
  if (!isAdmin.value) return
  try {
    const res = await http.get('/api/auth/users')
    aclUserOptions.value = res.data || []
  } catch {
    aclUserOptions.value = []
  }
  try {
    const res = await http.get('/api/admin/groups')
    aclGroupOptions.value = (res.data.items || []).map((g: any) => g.name)
  } catch {
    // 无 group.manage/user.manage 时列表为空, 下拉仍可 allow-create 手输组名
    aclGroupOptions.value = []
  }
}

const saveSpaceSettings = async () => {
  const f = editSpaceForm.value
  if (!f.isDefault && !f.name.trim()) { ElMessage.warning('请输入空间名称'); return }
  savingSpaceEdit.value = true
  try {
    const payload: Record<string, unknown> = {
      visibility: f.visibility,
      acl_users: f.acl_users,
      acl_groups: f.acl_groups,
    }
    if (!f.isDefault) {
      // 默认空间仅允许调整可见性/ACL; 名称/图标/说明保持系统维护
      payload.name = f.name
      payload.icon = f.icon || ''
      payload.description = f.description
    }
    await http.put(`/api/wiki/spaces/${f.id}`, payload)
    spaceEditDialog.value = false
    ElMessage.success('已保存')
    await loadSpaces()
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    savingSpaceEdit.value = false
  }
}

const moveToSpace = async (spaceId: string | null) => {
  if (!current.value) return
  try {
    await http.put(`/api/wiki/${current.value.id}/space`, { space_id: spaceId })
    current.value.space_id = spaceId
    await Promise.all([loadSpaces(), loadList()])
    ElMessage.success('已移动')
  } catch {
    ElMessage.error('移动失败')
  }
}

watch(activeSpace, () => loadList())

// ---- Wiki 多级树 ----
const items = ref<{ id: string; title: string; parent_id: string | null; position: number; space_id: string | null }[]>([])
const collapsedWiki = ref<string[]>([])
const totalAll = computed(() => defaultCount.value + spaces.value.reduce((a, s) => a + s.count, 0))
const wikiTreeRows = computed(() => {
  const q = filterText.value.trim().toLowerCase()
  const source = q ? items.value.filter(p => (p.title || '').toLowerCase().includes(q)) : items.value
  const byId = new Map<string, any>()
  source.forEach(p => byId.set(p.id, p))
  const childrenOf = new Map<string | null, any[]>()
  for (const p of source) {
    const pid = p.parent_id && byId.has(p.parent_id) ? p.parent_id : null
    if (!childrenOf.has(pid)) childrenOf.set(pid, [])
    childrenOf.get(pid)!.push(p)
  }
  const rows: { page: any; depth: number; hasChildren: boolean; collapsed: boolean }[] = []
  const walk = (pid: string | null, depth: number) => {
    const kids = (childrenOf.get(pid) || []).slice().sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    for (const k of kids) {
      const hasChildren = (childrenOf.get(k.id) || []).length > 0
      const collapsed = collapsedWiki.value.includes(k.id)
      rows.push({ page: k, depth, hasChildren, collapsed })
      if (!collapsed) walk(k.id, depth + 1)
    }
  }
  walk(null, 0)
  return rows
})
const toggleWiki = (id: string) => {
  collapsedWiki.value = collapsedWiki.value.includes(id)
    ? collapsedWiki.value.filter(x => x !== id)
    : [...collapsedWiki.value, id]
}
const createWikiPage = async (parentId: string | null = null) => {
  const spaceId = activeSpace.value && activeSpace.value !== 'default' ? activeSpace.value : null
  try {
    const res = await http.post('/api/wiki', { title: '无标题', space_id: spaceId, parent_id: parentId })
    await loadSpaces()
    await loadList()
    await openPage(res.data.id)
  } catch { ElMessage.error('创建失败') }
}
const deleteWikiPage = async (id: string) => {
  try { await ElMessageBox.confirm('确认删除该页面？其子页面将上移一级。', '删除', { type: 'warning' }) } catch { return }
  try {
    await http.delete(`/api/wiki/${id}`)
    if (current.value?.id === id) current.value = null
    await loadSpaces()
    await loadList()
    ElMessage.success('已删除')
  } catch { ElMessage.error('删除失败') }
}
const wikiDragId = ref<string | null>(null)
const wikiDrop = ref<{ id: string; zone: 'before' | 'after' | 'inside' } | null>(null)
const onWikiDragStart = (p: any, ev: DragEvent) => {
  if (!isAdmin.value) return
  wikiDragId.value = p.id
  if (ev.dataTransfer) { ev.dataTransfer.effectAllowed = 'move'; ev.dataTransfer.setData('text/plain', p.id) }
}
const onWikiDragOver = (p: any, ev: DragEvent) => {
  if (!wikiDragId.value || wikiDragId.value === p.id) return
  ev.preventDefault()
  const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect()
  const y = ev.clientY - rect.top
  const zone: 'before' | 'after' | 'inside' = y < rect.height * 0.3 ? 'before' : y > rect.height * 0.7 ? 'after' : 'inside'
  wikiDrop.value = { id: p.id, zone }
}
const onWikiDrop = async (p: any, ev: DragEvent) => {
  ev.preventDefault()
  if (!isAdmin.value) return
  const dragId = wikiDragId.value
  const t = wikiDrop.value
  wikiDragId.value = null
  wikiDrop.value = null
  if (!dragId || !t || dragId === p.id) return
  let parentId: string | null = null
  let position = 0
  if (t.zone === 'inside') {
    parentId = p.id
    position = items.value.filter(x => x.parent_id === p.id).length
  } else {
    parentId = p.parent_id ?? null
    const sib = items.value.filter(x => (x.parent_id ?? null) === parentId).sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
    const idx = sib.findIndex(x => x.id === p.id)
    position = t.zone === 'before' ? Math.max(0, idx) : idx + 1
  }
  try {
    await http.put(`/api/wiki/${dragId}/move`, { parent_id: parentId, position })
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '移动失败')
  }
}
const onWikiDragEnd = () => { wikiDragId.value = null; wikiDrop.value = null }

const openPage = async (pageId: string) => {
  try {
    const res = await http.get(`/api/wiki/${pageId}`)
    current.value = res.data
    groupInput.value = res.data.group_id || ''
    if (route.path !== `/wiki/${pageId}`) {
      router.replace({ path: `/wiki/${pageId}` })
    }
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载页面失败')
  }
}

const startEdit = () => {
  if (!current.value) return
  editForm.value = {
    content: current.value.content || '',
    summary: current.value.summary || '',
    category: current.value.category || '',
  }
  editing.value = true
}

const saveEdit = async () => {
  if (!current.value) return
  savingEdit.value = true
  try {
    await http.put(`/api/wiki/${current.value.id}`, editForm.value)
    current.value = { ...current.value, ...editForm.value }
    editing.value = false
    ElMessage.success('已保存')
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    savingEdit.value = false
  }
}

const saveGroup = async () => {
  if (!current.value) return
  savingGroup.value = true
  try {
    const res = await http.put(`/api/wiki/${current.value.id}/group`, { group_id: groupInput.value })
    current.value = { ...current.value, group_id: res.data.group_id }
    groupInput.value = res.data.group_id || ''
    ElMessage.success('已保存')
    await loadList()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    savingGroup.value = false
  }
}

const escapeAttr = (text: string) =>
  String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

const renderContent = (text: string) => {
  if (!text) return ''
  const withLinks = text.replace(/\[\[([^\]]+)\]\]/g, (_, raw: string) => {
    const { title, anchor } = splitWikiTarget(raw)
    const id = titleToId.value[title]
    if (id) {
      const anchorAttr = anchor ? ` data-anchor="${escapeAttr(anchor)}"` : ''
      const label = anchor ? `${title}#${anchor}` : title
      return `<span class="wiki-link" data-id="${id}"${anchorAttr}>${label}</span>`
    }
    return `<strong>${title}</strong>`
  })
  // 笔记可含 HTML(如提示框/折叠块/下划线/高亮),渲染前统一做白名单净化,防止 XSS
  return DOMPurify.sanitize(md.render(withLinks), {
    ADD_TAGS: ['details', 'summary'],
    ADD_ATTR: ['data-id', 'data-anchor', 'data-callout', 'data-toggle', 'open', 'target'],
  })
}

/** [[页面#锚点]] 跳转后滚动到标题 id(渲染时机不定, 有限重试) */
const scrollToAnchor = (anchor: string) => {
  const slug = String(anchor || '').trim()
  if (!slug) return
  nextTick(() => {
    let attempts = 0
    const tryScroll = () => {
      attempts++
      let el: HTMLElement | null = null
      try {
        el = (bodyRef.value?.querySelector(`[id="${CSS.escape(slug)}"]`) as HTMLElement | null) || null
      } catch { /* ignore */ }
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        el.classList.add('wiki-anchor-flash')
        window.setTimeout(() => el?.classList.remove('wiki-anchor-flash'), 1700)
        return
      }
      if (attempts < 20) window.setTimeout(tryScroll, 150)
    }
    tryScroll()
  })
}

const handleContentClick = async (e: MouseEvent) => {
  const target = (e.target as HTMLElement).closest('.wiki-link') as HTMLElement | null
  if (!target || !target.dataset.id) return
  const anchor = target.dataset.anchor || ''
  if (current.value?.id === target.dataset.id) {
    scrollToAnchor(anchor)
    return
  }
  await openPage(target.dataset.id)
  if (anchor && current.value?.id === target.dataset.id) scrollToAnchor(anchor)
}

// ---------------- wiki-link 悬浮预览 ----------------
const preview = reactive({ visible: false, title: '', snippet: '' })
const previewRef = shallowRef<HTMLElement>()
const previewCache = new Map<string, any>()
let previewSeq = 0

async function showPreview(el: HTMLElement) {
  const id = el.dataset.id || ''
  if (!id) return
  const anchor = el.dataset.anchor || ''
  previewRef.value = el
  preview.title = el.textContent || ''
  preview.snippet = '加载中…'
  preview.visible = true
  const seq = ++previewSeq
  try {
    let data = previewCache.get(id)
    if (!data) {
      data = (await http.get(`/api/wiki/${id}`)).data
      previewCache.set(id, data)
    }
    if (seq !== previewSeq || !preview.visible) return
    preview.title = data.title || preview.title
    const content = String(data.content || '')
    preview.snippet = anchor
      ? (extractHeadingSection(content, anchor) || data.summary || markdownSnippet(content))
      : (data.summary || markdownSnippet(content))
  } catch {
    // 加载失败静默关闭
    if (seq === previewSeq) preview.visible = false
  }
}

function hidePreview() {
  previewSeq++
  preview.visible = false
}

function onBodyMouseOver(ev: MouseEvent) {
  const link = (ev.target as HTMLElement | null)?.closest?.('.wiki-link') as HTMLElement | null
  if (!link || !bodyRef.value?.contains(link)) return
  if (preview.visible && previewRef.value === link) return
  void showPreview(link)
}

const handleImgError = (e: Event) => {
  const el = e.target as HTMLImageElement
  // 首次失败：请后端签名后重试；已尝试过或无需签名则直接隐藏，避免死循环。
  if (!signImageElement(el)) el.style.display = 'none'
}

// ---- 正文图片/图表惰性渲染: 进入视口(含预加载边距)才请求签名地址 / 渲染 mermaid ----
let bodyImgObserver: LazyObserver | null = null
let mermaidObserver: LazyObserver | null = null
const mermaidCache = new Map<string, string>()

/** 正文渲染完成后: 图片补 loading="lazy" 并延迟签名; mermaid 代码块延迟渲染为图。 */
const setupLazyBody = () => {
  const root = bodyRef.value
  if (!root) return
  bodyImgObserver?.disconnect()
  bodyImgObserver = createLazyObserver((el) => {
    signImageElement(el as HTMLImageElement)
  }, '280px 0px')
  observeLazyImages(root, bodyImgObserver)
  setupLazyMermaid(root)
}

const renderMermaidBlock = async (block: Element) => {
  const codeEl = block.querySelector('code')
  const codeText = codeEl?.textContent || ''
  if (!codeText.trim()) return
  try {
    let svg = mermaidCache.get(codeText)
    if (!svg) {
      const id = `mermaid-${Math.random().toString(36).slice(2, 11)}`
      const rendered = await mermaid.render(id, codeText)
      svg = rendered.svg
      mermaidCache.set(codeText, svg)
    }
    let target = block.nextElementSibling as HTMLElement | null
    if (!target || !target.classList.contains('mermaid-diagram')) {
      target = document.createElement('div')
      target.className = 'mermaid-diagram'
      block.parentElement?.insertBefore(target, block.nextSibling)
    }
    target.innerHTML = svg
  } catch (e) {
    // 语法错误等: 保留原代码块展示, 不破坏阅读
    console.error('Mermaid error:', e)
  }
}

const setupLazyMermaid = (root: HTMLElement) => {
  const blocks = root.querySelectorAll('pre > code.language-mermaid')
  if (!blocks.length) return
  mermaidObserver?.disconnect()
  mermaidObserver = createLazyObserver((el) => {
    void renderMermaidBlock(el)
  }, '300px 0px')
  blocks.forEach((code) => {
    const pre = code.parentElement
    if (!pre) return
    const codeText = code.textContent || ''
    const rendered = pre.nextElementSibling as HTMLElement | null
    if (rendered?.classList.contains('mermaid-diagram') && rendered.innerHTML === mermaidCache.get(codeText)) return
    mermaidObserver!.observe(pre)
  })
}

// 正文渲染完成后替换图片为签名地址 / 惰性渲染图表。
watch(current, () => {
  nextTick(() => { setupLazyBody() })
})

const pollStatus = async () => {
  try {
    const res = await http.get('/api/wiki/rebuild-status')
    status.value = res.data
    running.value = !!res.data.running
    if (!res.data.running) {
      stopPolling()
      await loadList()
      if (current.value) {
        const fresh = await http.get(`/api/wiki/${current.value.id}`).catch(() => null)
        if (fresh) {
          current.value = fresh.data
          groupInput.value = fresh.data.group_id || ''
        }
      }
    }
  } catch { /* ignore */ }
}

const startPolling = () => {
  if (pollTimer) return
  pollTimer = window.setInterval(pollStatus, 3000)
}
const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

watch(
  () => route.params.id,
  (id) => {
    if (id) openPage(String(id))
  }
)

onMounted(async () => {
  await Promise.all([loadSpaces(), loadList()])
  const id = route.params.id
  if (id) {
    await openPage(String(id))
  }
  if (running.value) startPolling()
  else await pollStatus()
})

onBeforeUnmount(() => {
  stopPolling()
  bodyImgObserver?.disconnect()
  bodyImgObserver = null
  mermaidObserver?.disconnect()
  mermaidObserver = null
})
</script>

<style scoped>
@import 'highlight.js/styles/github.css';

.wiki-page {
  height: 100%;
  display: flex;
  background: var(--bg);
}
.wiki-sidebar {
  width: 300px;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.wiki-sidebar-header {
  padding: 16px 20px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.wiki-brand {
  font-weight: 700;
  font-size: 15px;
  color: #1e293b;
}
.wiki-filter {
  padding: 0 16px 8px;
}
.wiki-cat-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 12px 16px;
}
.wiki-cat {
  margin-bottom: 6px;
}
.wiki-cat-name {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-3);
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.wiki-cat-name:hover { background: var(--surface-2); color: var(--text-2); }
.wiki-cat-chevron {
  display: inline-flex;
  color: var(--text-3);
  font-size: 12px;
  transition: transform 0.15s;
}
.wiki-cat-chevron.open { transform: rotate(90deg); }
.wiki-cat-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wiki-cat-count {
  background: var(--surface-3);
  color: var(--text-3);
  border-radius: 8px;
  padding: 0 6px;
  font-size: 11px;
  margin-left: 4px;
}
.wiki-page-item {
  padding: 7px 12px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 13px;
  color: #334155;
  transition: all 0.12s;
}
.wiki-page-item:hover {
  background: #f1f5f9;
}
.wiki-page-item.active {
  background: #eff6ff;
  color: #2563eb;
  font-weight: 500;
}
.wiki-main {
  flex: 1;
  overflow-y: auto;
  padding: 28px 48px;
}
.wiki-content-card {
  max-width: 860px;
  margin: 0 auto;
  background: var(--surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--border);
  padding: 36px 48px;
  min-height: 70vh;
}
.wiki-card-actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 4px;
}
.wiki-edit-tip {
  font-size: 12px;
  color: #94a3b8;
}
.wiki-edit-field {
  margin-bottom: 8px;
}
.wiki-edit-content :deep(textarea) {
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 13px;
  line-height: 1.6;
}
.wiki-edit-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.wiki-crumb {
  font-size: 12px;
  color: #94a3b8;
  margin-bottom: 8px;
}
.wiki-title {
  font-size: 28px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 10px;
}
.wiki-summary {
  background: var(--surface-2);
  border-left: 3px solid var(--primary);
  padding: 10px 14px;
  border-radius: 0 8px 8px 0;
  color: var(--text-2);
  font-size: 13px;
  margin-bottom: 20px;
  line-height: 1.6;
}
.wiki-group-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 18px;
  padding: 8px 12px;
  background: #f8fafc;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
}
.wiki-group-label {
  font-size: 12px;
  color: #475569;
  white-space: nowrap;
}
.wiki-group-input {
  max-width: 220px;
}
.wiki-body {
  font-size: 15px;
  line-height: 1.8;
  color: #1f2937;
}
.wiki-body :deep(h1), .wiki-body :deep(h2), .wiki-body :deep(h3) {
  color: #111827;
  margin: 24px 0 10px;
}
.wiki-body :deep(h1) { font-size: 24px; }
.wiki-body :deep(h2) { font-size: 20px; border-bottom: 1px solid #f1f5f9; padding-bottom: 6px; }
.wiki-body :deep(h3) { font-size: 17px; }
.wiki-body :deep(p) { margin: 10px 0; }
.wiki-body :deep(pre) {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 14px 16px;
  overflow-x: auto;
}
.wiki-body :deep(code) {
  background: #f1f5f9;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}
.wiki-body :deep(pre code) {
  background: none;
  padding: 0;
}
.wiki-body :deep(.mermaid-diagram) {
  margin: 10px 0 16px;
  padding: 16px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  text-align: center;
  overflow-x: auto;
}
.wiki-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 14px 0;
}
.wiki-body :deep(th), .wiki-body :deep(td) {
  border: 1px solid #e2e8f0;
  padding: 8px 12px;
  text-align: left;
}
.wiki-body :deep(th) { background: #f8fafc; }
.wiki-body :deep(blockquote) {
  border-left: 3px solid #e2e8f0;
  padding-left: 14px;
  color: #64748b;
  margin: 10px 0;
}
.wiki-body :deep(img) {
  max-width: 100%;
  border-radius: 8px;
}
.wiki-body :deep(ul.contains-task-list) {
  list-style: none;
  padding-left: 4px;
}
.wiki-body :deep(li.task-list-item) {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.wiki-body :deep(li.task-list-item input[type='checkbox']) {
  margin-top: 5px;
  accent-color: #2563eb;
}
.wiki-body :deep(mark) {
  background: #fef08a;
  padding: 1px 3px;
  border-radius: 3px;
}
.wiki-body :deep(u) {
  text-decoration: underline;
  text-underline-offset: 2px;
}
.wiki-body :deep(.callout) {
  border-radius: 10px;
  padding: 12px 16px;
  margin: 14px 0;
  border-left: 4px solid #3b82f6;
  background: #eff6ff;
}
.wiki-body :deep(.callout > *:first-child) { margin-top: 0; }
.wiki-body :deep(.callout > *:last-child) { margin-bottom: 0; }
.wiki-body :deep(.callout-tip) { border-left-color: #8b5cf6; background: #f5f3ff; }
.wiki-body :deep(.callout-success) { border-left-color: #10b981; background: #ecfdf5; }
.wiki-body :deep(.callout-warn) { border-left-color: #f59e0b; background: #fffbeb; }
.wiki-body :deep(.callout-danger) { border-left-color: #ef4444; background: #fef2f2; }
.wiki-body :deep(details[data-toggle]) {
  margin: 10px 0;
  padding: 6px 0;
}
.wiki-body :deep(details[data-toggle] > summary) {
  cursor: pointer;
  font-weight: 600;
  color: #1e293b;
  padding: 4px 0;
  list-style: none;
}
.wiki-body :deep(details[data-toggle] > summary::-webkit-details-marker) { display: none; }
.wiki-body :deep(details[data-toggle] > summary::before) {
  content: '▸';
  display: inline-block;
  margin-right: 6px;
  color: #94a3b8;
  transition: transform 0.15s;
}
.wiki-body :deep(details[data-toggle][open] > summary::before) { transform: rotate(90deg); }
.wiki-body :deep(details[data-toggle] .toggle-content) {
  padding-left: 20px;
  border-left: 2px solid #eef2f7;
  margin-left: 6px;
}
.wiki-body :deep(.wiki-link) {
  color: #2563eb;
  text-decoration: none;
  border-bottom: 1px dashed #93c5fd;
  cursor: pointer;
}
.wiki-body :deep(.wiki-link:hover) {
  color: #1d4ed8;
  border-bottom-style: solid;
}
.wiki-body :deep(.wiki-anchor-flash) {
  animation: wiki-anchor-flash 1.6s ease;
}
@keyframes wiki-anchor-flash {
  0%, 100% { background: transparent; }
  30%, 70% { background: rgba(250, 204, 21, 0.35); }
}
.wiki-sources {
  margin-top: 32px;
  padding-top: 16px;
  border-top: 1px solid #f1f5f9;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.wiki-sources-label {
  font-size: 12px;
  color: #94a3b8;
}
.wiki-source-link {
  font-size: 12px;
  color: #2563eb;
  background: #eff6ff;
  padding: 2px 10px;
  border-radius: 10px;
  text-decoration: none;
}
.wiki-source-link:hover {
  background: #dbeafe;
}
.wiki-empty {
  text-align: center;
  margin-top: 140px;
  color: #94a3b8;
}
.wiki-empty h2 {
  font-size: 18px;
  color: #64748b;
  margin-bottom: 8px;
}
.wiki-progress {
  font-size: 13px;
}

/* 空间选择 */
.space-bar { padding: 4px 8px 8px; display: flex; flex-direction: column; gap: 1px; }
.space-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-2);
}
.space-item:hover { background: var(--surface-2); }
.space-item.active { background: var(--surface-3); color: var(--text); font-weight: 500; }
.space-icon { flex: 0 0 auto; }
.space-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.space-count { font-size: 11px; color: var(--text-3); }
.space-menu {
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: 13px;
  line-height: 1;
  padding: 2px 4px;
  border-radius: 4px;
  cursor: pointer;
  visibility: hidden;
}
.space-item:hover .space-menu { visibility: visible; }
.space-menu:hover { background: var(--surface-3); color: var(--text); }
.space-add {
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: 12px;
  text-align: left;
  padding: 5px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.space-add:hover { background: var(--surface-2); color: var(--text-2); }

/* 多级树 */
.wiki-tree { padding: 2px 4px; }
.wiki-tree-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 5px 6px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: var(--text-2);
}
.wiki-tree-item:hover { background: var(--surface-2); }
.wiki-tree-item.active { background: var(--surface-3); color: var(--text); font-weight: 500; }
.wiki-tree-chev {
  width: 14px;
  display: inline-flex;
  justify-content: center;
  color: var(--text-3);
  font-size: 12px;
  transition: transform 0.15s;
  flex: 0 0 auto;
}
.wiki-tree-chev.open { transform: rotate(90deg); }
.wiki-tree-chev.placeholder { visibility: hidden; }
.wiki-tree-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wiki-tree-menu {
  opacity: 0;
  border: none;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  border-radius: 4px;
  padding: 0 5px;
  line-height: 1.4;
}
.wiki-tree-item:hover .wiki-tree-menu { opacity: 1; }
.wiki-tree-menu:hover { background: var(--surface-3); color: var(--text); }
.wiki-tree-item.dragging { opacity: 0.5; }
.wiki-tree-item.drop-before { box-shadow: inset 0 2px 0 var(--primary); }
.wiki-tree-item.drop-after { box-shadow: inset 0 -2px 0 var(--primary); }
.wiki-tree-item.drop-inside { background: var(--primary-weak); outline: 1px dashed var(--primary); outline-offset: -2px; }
.wiki-tree-add {
  color: var(--text-3);
  font-size: 13px;
  padding: 6px 10px;
  border-radius: 6px;
  cursor: pointer;
}
.wiki-tree-add:hover { background: var(--surface-2); color: var(--text-2); }
</style>

<style>
/* wiki-link 悬浮预览(teleport 到 body, 需全局样式) */
.wiki-preview {
  max-width: 340px;
}
.wiki-preview .wiki-preview-title {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wiki-preview .wiki-preview-snippet {
  font-size: 12px;
  line-height: 1.6;
  color: #6b7280;
  max-height: 96px;
  overflow: hidden;
  word-break: break-word;
}
</style>
