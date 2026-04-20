<template>
  <div class="app-layout">
    <nav class="nav-bar" v-if="showNav">
      <div class="nav-brand">Notes RAG</div>
      <div class="nav-links">
        <router-link to="/" class="nav-link" active-class="active">笔记</router-link>
        <router-link to="/graph" class="nav-link" active-class="active">知识图谱</router-link>
      </div>
    </nav>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const showNav = computed(() => route.path !== '/')
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
.app-main {
  flex: 1;
  overflow: hidden;
}
</style>
