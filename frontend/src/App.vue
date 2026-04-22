<template>
  <div class="app-layout">
    <nav class="nav-bar" v-if="showNav">
      <div class="nav-brand">Notes RAG</div>
      <div class="nav-links">
        <router-link to="/" class="nav-link" active-class="active">笔记</router-link>
        <router-link to="/graph" class="nav-link" active-class="active">知识图谱</router-link>
      </div>
      <div class="nav-user" v-if="isLoggedIn">
        <span class="nav-username">{{ authStore.user?.display_name || authStore.user?.username }}</span>
        <el-button size="small" @click="handleLogout">退出</el-button>
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
const showNav = computed(() => route.path !== '/' && route.path !== '/login')
const isLoggedIn = computed(() => authStore.isLoggedIn)

const handleLogout = () => {
  authStore.logout()
  router.push('/login')
}
</script>

<style>
body {
  margin: 0;
  padding: 0;
}
.app-layout {
  height: 100vh;
  display: flex;
  flex-direction: column;
}
.nav-bar {
  height: 48px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 24px;
}
.nav-brand {
  font-weight: 700;
  font-size: 16px;
  color: #1f2937;
}
.nav-links {
  display: flex;
  gap: 4px;
}
.nav-link {
  padding: 6px 16px;
  border-radius: 6px;
  text-decoration: none;
  color: #606266;
  font-size: 14px;
  transition: all 0.2s;
}
.nav-link:hover {
  background: #f5f7fa;
  color: #409eff;
}
.nav-link.active {
  background: #ecf5ff;
  color: #409eff;
  font-weight: 500;
}
.nav-user {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}
.nav-username {
  font-size: 14px;
  color: #606266;
}
.app-main {
  flex: 1;
  overflow: hidden;
}
</style>
