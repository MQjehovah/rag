<template>
  <div class="app-layout">
    <nav class="nav-bar" v-if="showNav">
      <div class="nav-brand">
        <span class="nav-logo">R</span>
        <span class="nav-brand-text">知识库</span>
      </div>
      <div class="nav-links">
        <router-link to="/" class="nav-link" active-class="active">AI 问答</router-link>
        <router-link to="/notes" class="nav-link" active-class="active">笔记</router-link>
        <router-link to="/wiki" class="nav-link" active-class="active">知识库</router-link>
        <router-link to="/pipelines" class="nav-link" active-class="active">编译管道</router-link>
        <router-link to="/sources" class="nav-link" active-class="active">数据源</router-link>
        <router-link to="/graph" class="nav-link" active-class="active">知识图谱</router-link>
        <router-link to="/embeddings" class="nav-link" active-class="active">嵌入模型</router-link>
      </div>
      <div class="nav-user" v-if="isLoggedIn">
        <el-dropdown trigger="click">
          <span class="nav-user-trigger">
            <span class="nav-avatar">{{ (authStore.user?.display_name || authStore.user?.username || '?')[0] }}</span>
            <span class="nav-username">{{ authStore.user?.display_name || authStore.user?.username }}</span>
            <span class="nav-caret">▾</span>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="handleLogout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </nav>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const showNav = computed(() => route.path !== '/login' && !route.path.startsWith('/share'))
const isLoggedIn = computed(() => authStore.isLoggedIn)

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
  flex-direction: column;
  background: var(--bg, #f6f7fb);
}
.nav-bar {
  height: 56px;
  flex: 0 0 auto;
  background: rgba(255, 255, 255, 0.82);
  backdrop-filter: saturate(180%) blur(12px);
  -webkit-backdrop-filter: saturate(180%) blur(12px);
  border-bottom: 1px solid var(--border, #e6e8f0);
  display: flex;
  align-items: center;
  padding: 0 18px;
  gap: 20px;
  z-index: 100;
}
.nav-brand {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: 0 0 auto;
}
.nav-logo {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #6366f1, #4f46e5);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 2px 8px rgba(79, 70, 229, 0.35);
}
.nav-brand-text {
  font-weight: 700;
  font-size: 15px;
  color: var(--text, #1f2430);
  letter-spacing: -0.2px;
}
.nav-links {
  display: flex;
  gap: 2px;
  flex: 1 1 auto;
  overflow-x: auto;
  scrollbar-width: none;
}
.nav-links::-webkit-scrollbar { display: none; }
.nav-link {
  padding: 7px 13px;
  border-radius: 8px;
  text-decoration: none;
  color: var(--text-2, #59616f);
  font-size: 13.5px;
  font-weight: 500;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
}
.nav-link:hover {
  background: var(--surface-2, #f2f4f9);
  color: var(--text, #1f2430);
}
.nav-link.active {
  background: var(--primary-weak, #eef0ff);
  color: var(--primary, #4f46e5);
}
.nav-user {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
}
.nav-user-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px 4px 4px;
  border-radius: 9px;
  transition: background 0.15s;
}
.nav-user-trigger:hover {
  background: var(--surface-2, #f2f4f9);
}
.nav-avatar {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #38bdf8, #6366f1);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.nav-username {
  font-size: 13px;
  color: var(--text-2, #59616f);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.nav-caret {
  font-size: 10px;
  color: var(--text-3, #8b93a4);
}
.app-main {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
</style>
