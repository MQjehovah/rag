<template>
  <div class="app-layout">
    <nav class="nav-bar" v-if="showNav">
      <div class="nav-brand">Notes RAG</div>
      <div class="nav-links">
        <router-link to="/" class="nav-link" active-class="active">AI 问答</router-link>
        <router-link to="/notes" class="nav-link" active-class="active">笔记</router-link>
        <router-link to="/graph" class="nav-link" active-class="active">知识图谱</router-link>
      </div>
      <div class="nav-user" v-if="isLoggedIn">
        <el-dropdown trigger="click">
          <span class="nav-user-trigger">
            <span class="nav-avatar">{{ (authStore.user?.display_name || authStore.user?.username || '?')[0] }}</span>
            <span class="nav-username">{{ authStore.user?.display_name || authStore.user?.username }}</span>
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
const showNav = computed(() => route.path !== '/login')
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
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  -webkit-font-smoothing: antialiased;
}
.app-layout {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #f0f2f5;
}
.nav-bar {
  height: 52px;
  background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
  display: flex;
  align-items: center;
  padding: 0 24px;
  gap: 32px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
  z-index: 100;
}
.nav-brand {
  font-weight: 700;
  font-size: 17px;
  color: #fff;
  letter-spacing: -0.3px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.nav-brand::before {
  content: '';
  width: 8px;
  height: 8px;
  background: #38bdf8;
  border-radius: 50%;
  display: inline-block;
}
.nav-links {
  display: flex;
  gap: 2px;
}
.nav-link {
  padding: 7px 18px;
  border-radius: 8px;
  text-decoration: none;
  color: #94a3b8;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.2s;
}
.nav-link:hover {
  background: rgba(255,255,255,0.08);
  color: #e2e8f0;
}
.nav-link.active {
  background: rgba(56,189,248,0.15);
  color: #38bdf8;
}
.nav-user {
  margin-left: auto;
  display: flex;
  align-items: center;
}
.nav-user-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 8px;
  transition: background 0.15s;
}
.nav-user-trigger:hover {
  background: rgba(255,255,255,0.08);
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
  color: #94a3b8;
}
.app-main {
  flex: 1;
  overflow: hidden;
}
</style>
