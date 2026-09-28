<template>
  <div class="app-layout" :class="{ 'rail-collapsed': collapsed }">
    <template v-if="showNav">
      <aside class="rail">
        <div class="rail-head">
          <button class="ws-btn" :title="collapsed ? '展开导航' : '企业知识库'" @click="collapsed && toggleCollapse()">
            <span class="ws-logo">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></svg>
            </span>
            <span class="ws-name">企业知识库</span>
            <svg class="ws-chevron" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6" /></svg>
          </button>
          <button class="rail-collapse" title="收起导航" @click="toggleCollapse">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></svg>
          </button>
        </div>

        <div class="rail-scroll">
          <button class="rail-item as-btn rail-search" :title="collapsed ? '搜索' : ''" @click="openSearch">
            <span class="rail-item-icon">
              <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
            </span>
            <span class="rail-item-label">搜索</span>
            <span class="rail-kbd">Ctrl K</span>
          </button>
          <div v-for="grp in visibleGroups" :key="grp.label" class="rail-section">
            <div class="rail-section-label">{{ grp.label }}</div>
            <router-link
              v-for="item in grp.items"
              :key="item.path"
              :to="item.path"
              class="rail-item"
              active-class="active"
              :title="collapsed ? item.label : ''"
            >
              <span class="rail-item-icon">
                <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" v-html="item.svg" />
              </span>
              <span class="rail-item-label">{{ item.label }}</span>
            </router-link>
          </div>
        </div>

        <div class="rail-foot">
          <el-dropdown trigger="click" placement="top-start" @command="applyTheme">
            <button class="rail-item as-btn" :title="`主题：${themeLabel}`">
              <span class="rail-item-icon">
                <svg v-if="theme === 'dark'" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>
                <svg v-else viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
              </span>
              <span class="rail-item-label">{{ themeLabel }}</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="light">亮色</el-dropdown-item>
                <el-dropdown-item command="dark">深色</el-dropdown-item>
                <el-dropdown-item command="system">跟随系统</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <el-dropdown v-if="isLoggedIn" trigger="click" placement="top-start">
            <span class="rail-user">
              <span class="rail-avatar">{{ initial }}</span>
              <span class="rail-username">{{ userName }}</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="handleLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </aside>
    </template>

    <main class="content">
      <router-view />
    </main>

    <!-- 全局搜索 (Ctrl+K) -->
    <Teleport to="body">
      <div v-if="searchOpen" class="qs-overlay" @click.self="searchOpen = false">
        <div class="qs-box">
          <input
            ref="searchInput"
            v-model="searchQuery"
            class="qs-input"
            placeholder="搜索页面…（↑↓ 选择，回车打开，Esc 关闭）"
            @keydown="onSearchKey"
          />
          <div class="qs-list">
            <div
              v-for="(p, i) in searchResults"
              :key="p.id"
              class="qs-item"
              :class="{ active: i === searchIndex }"
              @mouseenter="searchIndex = i"
              @click="pickSearch(p)"
            >{{ p.title }}</div>
            <div v-if="!searchResults.length" class="qs-empty">无匹配页面</div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { PERM } from './constants/perms'
import type { Permission } from './constants/perms'
import http from './api/http'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

type NavItem = { path: string; label: string; svg: string; perm?: Permission }

const navGroups: { label: string; items: NavItem[] }[] = [
  {
    label: '工作区',
    items: [
      { path: '/', label: 'AI 问答', svg: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z"/>' },
      { path: '/notes', label: '笔记', svg: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>' },
      { path: '/wiki', label: '知识库', svg: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>' },
    ],
  },
  {
    label: '数据与管道',
    items: [
      { path: '/sources', label: '数据源', perm: PERM.sources, svg: '<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0V8zM12 18v4"/>' },
      { path: '/pipelines', label: '编译管道', perm: PERM.pipeline, svg: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M6 9v6M18 6a9 9 0 0 1-9 9"/>' },
      { path: '/templates', label: '编译模板', perm: PERM.pipeline, svg: '<path d="M4 4h16v4H4zM4 10h16v4H4zM4 16h10v4H4z"/>' },
      { path: '/graph', label: '知识图谱', perm: PERM.graph, svg: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 10.7l6.8-4M8.6 13.3l6.8 4"/>' },
      { path: '/embeddings', label: '嵌入模型', perm: PERM.embedding, svg: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M20 9h2M20 14h2M2 9h2M2 14h2"/>' },
    ],
  },
  {
    label: '系统管理',
    items: [
      { path: '/admin/users', label: '用户管理', perm: PERM.user, svg: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>' },
      { path: '/admin/roles', label: '角色权限', perm: PERM.role, svg: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>' },
      { path: '/admin/groups', label: '组管理', perm: PERM.group, svg: '<path d="M17 21v-2a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>' },
    ],
  },
]

const visibleGroups = computed(() =>
  navGroups
    .map(g => ({ label: g.label, items: g.items.filter(it => !it.perm || authStore.hasPerm(it.perm)) }))
    .filter(g => g.items.length > 0)
)

const showNav = computed(() => route.path !== '/login' && !route.path.startsWith('/share'))
const isLoggedIn = computed(() => authStore.isLoggedIn)
const userName = computed(() => authStore.user?.display_name || authStore.user?.username || '')
const initial = computed(() => (userName.value || '?')[0])

const collapsed = ref(localStorage.getItem('rag-rail-collapsed') === '1')
const toggleCollapse = () => {
  collapsed.value = !collapsed.value
  localStorage.setItem('rag-rail-collapsed', collapsed.value ? '1' : '0')
}

type Theme = 'light' | 'dark' | 'system'
const theme = ref<Theme>((localStorage.getItem('rag-theme') as Theme) || 'light')
const themeLabel = computed(() => (theme.value === 'dark' ? '深色' : theme.value === 'system' ? '跟随系统' : '亮色'))
const applyTheme = (t: Theme) => {
  theme.value = t
  localStorage.setItem('rag-theme', t)
  const dark = t === 'dark' || (t === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
}

const handleLogout = () => {
  authStore.logout()
  router.push('/login')
}

// ---- 全局搜索 ----
const searchOpen = ref(false)
const searchQuery = ref('')
const searchIndex = ref(0)
const searchInput = ref<HTMLInputElement | null>(null)
const searchAll = ref<{ id: string; title: string }[]>([])
let searchLoaded = false

const searchResults = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return searchAll.value.slice(0, 40)
  return searchAll.value.filter(p => p.title.toLowerCase().includes(q)).slice(0, 40)
})

const openSearch = async () => {
  searchOpen.value = true
  searchQuery.value = ''
  searchIndex.value = 0
  nextTick(() => searchInput.value?.focus())
  if (!searchLoaded) {
    try {
      const res = await http.get('/api/pages', { params: { page: 1, page_size: 500 } })
      searchAll.value = (res.data.items || []).map((p: any) => ({ id: p.id, title: p.title || '无标题' }))
      searchLoaded = true
    } catch { /* ignore */ }
  }
}
const pickSearch = (p: { id: string }) => {
  searchOpen.value = false
  router.push({ path: '/notes', query: { page: p.id } })
}
const onSearchKey = (e: KeyboardEvent) => {
  const list = searchResults.value
  if (e.key === 'ArrowDown') { e.preventDefault(); searchIndex.value = Math.min(searchIndex.value + 1, list.length - 1) }
  else if (e.key === 'ArrowUp') { e.preventDefault(); searchIndex.value = Math.max(searchIndex.value - 1, 0) }
  else if (e.key === 'Enter') { e.preventDefault(); if (list[searchIndex.value]) pickSearch(list[searchIndex.value]) }
  else if (e.key === 'Escape') { searchOpen.value = false }
}
const onGlobalKey = (e: KeyboardEvent) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    openSearch()
  }
}
onMounted(() => window.addEventListener('keydown', onGlobalKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKey))
</script>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.app-layout { height: 100vh; display: flex; overflow: hidden; background: var(--bg); }

/* 左侧导航 (Notion 侧栏) */
.rail {
  width: 240px;
  flex: 0 0 auto;
  height: 100vh;
  background: var(--bg);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  transition: width 0.16s ease;
  overflow: hidden;
}
.rail-head {
  height: 46px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px;
}
.ws-btn {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  border: none;
  background: transparent;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--text);
}
.ws-btn:hover { background: var(--surface-hover); }
.ws-logo {
  width: 24px;
  height: 24px;
  flex: 0 0 auto;
  border-radius: 7px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  background: linear-gradient(135deg, #3b82f6, #6366f1);
  box-shadow: 0 1px 3px rgba(59, 130, 246, 0.35);
}
.ws-name { flex: 1; text-align: left; font-weight: 600; font-size: 14px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ws-chevron { color: var(--text-3); flex: 0 0 auto; }
.rail-collapse {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border: none;
  background: transparent;
  color: var(--text-3);
  border-radius: 6px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.rail-collapse:hover { background: var(--surface-hover); color: var(--text); }

.rail-scroll { flex: 1; overflow-y: auto; padding: 4px 8px 8px; }
.rail-section { margin-bottom: 2px; }
.rail-section-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  padding: 8px 10px 3px;
  letter-spacing: 0.02em;
}
.rail-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 10px;
  height: 30px;
  border-radius: 5px;
  text-decoration: none;
  color: var(--text-2);
  font-size: 14px;
  font-weight: 400;
  white-space: nowrap;
  transition: background 0.1s, color 0.1s;
}
.rail-item-icon { display: inline-flex; width: 17px; flex: 0 0 auto; opacity: 0.9; }
.rail-item:hover { background: var(--surface-hover); color: var(--text); }
.rail-item.active { background: var(--surface-3); color: var(--text); font-weight: 500; }
.rail-item.as-btn { width: 100%; border: none; background: transparent; cursor: pointer; text-align: left; }

.rail-foot {
  flex: 0 0 auto;
  border-top: 1px solid var(--border);
  padding: 6px 8px 10px;
}
.rail-user {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 10px;
  height: 30px;
  border-radius: 5px;
  cursor: pointer;
}
.rail-user:hover { background: var(--surface-hover); }
.rail-avatar {
  width: 20px;
  height: 20px;
  flex: 0 0 auto;
  border-radius: 5px;
  background: linear-gradient(135deg, #38bdf8, #6366f1);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.rail-username { font-size: 13px; color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 收起态 */
.app-layout.rail-collapsed .rail { width: 60px; }
.app-layout.rail-collapsed .ws-name,
.app-layout.rail-collapsed .ws-chevron,
.app-layout.rail-collapsed .rail-section-label,
.app-layout.rail-collapsed .rail-item-label,
.app-layout.rail-collapsed .rail-username { display: none; }
.app-layout.rail-collapsed .rail-collapse { display: none; }
.app-layout.rail-collapsed .rail-head { justify-content: center; padding: 0 4px; }
.app-layout.rail-collapsed .ws-btn { flex: 0 0 auto; padding: 6px; }
.app-layout.rail-collapsed .rail-item,
.app-layout.rail-collapsed .rail-user { justify-content: center; padding: 5px 0; }

.content { flex: 1; min-width: 0; height: 100vh; overflow: hidden; }

/* 搜索入口 + 全局搜索面板 */
.rail-search { margin-bottom: 4px; }
.rail-kbd {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 5px;
}
.app-layout.rail-collapsed .rail-kbd { display: none; }

.qs-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 15, 15, 0.24);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  z-index: 3000;
  padding-top: 12vh;
}
.qs-box {
  width: 560px;
  max-width: 92vw;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}
.qs-input {
  width: 100%;
  border: none;
  outline: none;
  background: transparent;
  padding: 16px 18px;
  font-size: 16px;
  color: var(--text);
  border-bottom: 1px solid var(--border);
}
.qs-list { max-height: 52vh; overflow-y: auto; padding: 6px; }
.qs-item {
  padding: 9px 12px;
  border-radius: 6px;
  font-size: 14px;
  color: var(--text);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.qs-item.active { background: var(--surface-2); }
.qs-empty { padding: 12px 14px; color: var(--text-3); font-size: 13px; }
</style>
