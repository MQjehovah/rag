<template>
  <div class="governance-layout">
    <header class="governance-header">
      <h1 class="governance-title">治理工作台</h1>
      <SectionNav :items="sectionItems" />
    </header>
    <main class="governance-body">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
// Phase G：治理工作台已从默认路由/导航退出。本布局作为历史文件保留，
// 不再引用已删除的治理导航配置，避免 vue-tsc 类型检查失败。
import SectionNav from '../components/navigation/SectionNav.vue'
import type { NavigationItem } from '../navigation/types'

const sectionItems: NavigationItem[] = []
</script>

<style scoped>
.governance-layout {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.governance-header {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.governance-title {
  font-size: 18px;
  font-weight: 700;
  color: #111827;
  margin: 0;
}
.governance-body {
  flex: 1;
  overflow: hidden;
  min-width: 0;
}
</style>
