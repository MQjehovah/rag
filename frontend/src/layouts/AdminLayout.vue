<template>
  <div class="admin-layout">
    <header class="admin-header">
      <h1 class="admin-title">系统工具</h1>
      <SectionNav :items="sectionItems" />
    </header>
    <main class="admin-body">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import SectionNav from '../components/navigation/SectionNav.vue'
import type { NavigationItem } from '../navigation/types'

const sectionItems: NavigationItem[] = [
  { key: 'retrieval', label: '检索评测', route: '/admin/retrieval', order: 1, roles: ['system_admin'] },
  { key: 'flags', label: 'Feature Flag', route: '/admin/flags', order: 3, roles: ['system_admin'] },
  { key: 'model-health', label: '模型健康', route: '/admin/model-health', order: 4, roles: ['system_admin'] },
]
</script>

<style scoped>
.admin-layout {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.admin-header {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.admin-title {
  font-size: 18px;
  font-weight: 700;
  color: #111827;
  margin: 0;
}
.admin-body {
  flex: 1;
  overflow: hidden;
  min-width: 0;
}
</style>
