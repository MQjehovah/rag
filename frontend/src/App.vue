<template>
  <div class="app-layout" :class="{ 'rail-collapsed': collapsed }">
    <template v-if="showNav">
      <aside class="rail">
        <div class="rail-head">
          <span class="rail-logo">R</span>
          <span class="rail-brand">企业知识库</span>
          <button class="rail-icon" title="收起导航" @click="toggleCollapse">«</button>
        </div>

        <nav class="rail-nav">
          <router-link
            v-for="item in navItems"
            :key="item.path"
            :to="item.path"
            class="rail-item"
            active-class="active"
            :title="collapsed ? item.label : ''"
          >
            <span class="rail-item-icon">{{ item.icon }}</span>
            <span class="rail-item-label">{{ item.label }}</span>
          </router-link>
        </nav>

        <div class="rail-foot">
          <el-dropdown trigger="click" placement="top-start" @command="applyTheme">
            <button class="rail-icon rail-theme" :title="`主题：${themeLabel}`">
              <span>{{ theme === 'dark' ? '🌙' : theme === 'system' ? '🖥' : '☀️' }}</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="light">☀️ 亮色</el-dropdown-item>
                <el-dropdown-item command="dark">🌙 深色</el-dropdown-item>
                <el-dropdown-item command="system">🖥 跟随系统</el-dropdown-item>
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
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const navItems = [
  { path: '/', label: 'AI 问答', icon: '💬' },
  { path: '/notes', label: '笔记', icon: '📝' },
  { path: '/wiki', label: '知识库', icon: '📚' },
  { path: '/pipelines', label: '编译管道', icon: '🔀' },
  { path: '/sources', label: '数据源', icon: '🔌' },
  { path: '/graph', label: '知识图谱', icon: '🕸' },
  { path: '/embeddings', label: '嵌入模型', icon: '🧬' },
]

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
</script>

<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.app-layout {
  height: 100vh;
  display: flex;
  overflow: hidden;
  background: var(--bg);
}

/* 左侧一级导航 */
.rail {
  width: 236px;
  flex: 0 0 auto;
  height: 100vh;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  transition: width 0.18s ease;
  overflow: hidden;
}
.rail-head {
  height: 56px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
}
.rail-logo {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border-radius: 8px;
  background: linear-gradient(135deg, #6366f1, #4f46e5);
  color: #fff;
  font-weight: 700;
  font-size: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 8px rgba(79, 70, 229, 0.35);
}
.rail-brand {
  flex: 1;
  font-weight: 700;
  font-size: 14.5px;
  color: var(--text);
  white-space: nowrap;
  letter-spacing: -0.2px;
}
.rail-icon {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border: none;
  background: transparent;
  color: var(--text-3);
  border-radius: 7px;
  cursor: pointer;
  font-size: 14px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.rail-icon:hover { background: var(--surface-2); color: var(--text); }

.rail-nav {
  flex: 1;
  overflow-y: auto;
  padding: 6px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rail-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  text-decoration: none;
  color: var(--text-2);
  font-size: 13.5px;
  font-weight: 500;
  white-space: nowrap;
  transition: background 0.14s, color 0.14s;
}
.rail-item-icon { width: 20px; text-align: center; font-size: 15px; flex: 0 0 auto; }
.rail-item:hover { background: var(--surface-2); color: var(--text); }
.rail-item.active { background: var(--primary-weak); color: var(--primary); }

.rail-foot {
  flex: 0 0 auto;
  border-top: 1px solid var(--border);
  padding: 8px 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.rail-theme { width: 100%; justify-content: flex-start; padding: 0 8px; gap: 8px; }
.rail-user {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 8px;
  border-radius: 8px;
  cursor: pointer;
}
.rail-user:hover { background: var(--surface-2); }
.rail-avatar {
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  border-radius: 7px;
  background: linear-gradient(135deg, #38bdf8, #6366f1);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.rail-username { font-size: 13px; color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 收起态: 仅图标 */
.app-layout.rail-collapsed .rail { width: 64px; }
.app-layout.rail-collapsed .rail-brand,
.app-layout.rail-collapsed .rail-username,
.app-layout.rail-collapsed .rail-item-label,
.app-layout.rail-collapsed .rail-theme span:not(:first-child) { display: none; }
.app-layout.rail-collapsed .rail-head { justify-content: center; padding: 0; }
.app-layout.rail-collapsed .rail-head .rail-icon { display: none; }
.app-layout.rail-collapsed .rail-item { justify-content: center; padding: 9px 0; }
.app-layout.rail-collapsed .rail-foot { align-items: center; }
.app-layout.rail-collapsed .rail-theme { width: 28px; padding: 0; justify-content: center; }
.app-layout.rail-collapsed .rail-user { padding: 5px 0; }

.content {
  flex: 1;
  min-width: 0;
  height: 100vh;
  overflow: hidden;
}
</style>
