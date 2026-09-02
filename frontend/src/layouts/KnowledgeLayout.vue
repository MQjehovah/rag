<template>
  <div class="knowledge-layout">
    <header class="knowledge-header">
      <h1 class="knowledge-title">知识中心</h1>
      <SectionNav :items="sectionItems" />
    </header>
    <main class="knowledge-body">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'
import { hasRole } from '../navigation/permissions'
import SectionNav from '../components/navigation/SectionNav.vue'
import { KNOWLEDGE_SECTIONS } from '../navigation/config'

const authStore = useAuthStore()

const sectionItems = computed(() =>
  KNOWLEDGE_SECTIONS.filter((item) => hasRole(authStore.user, item.roles))
)
</script>

<style scoped>
.knowledge-layout {
  height: 100%;
  display: flex;
  flex-direction: column;
  padding: 16px 24px;
  gap: 12px;
  overflow: hidden;
}
.knowledge-header {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.knowledge-title {
  font-size: 18px;
  font-weight: 700;
  color: #111827;
  margin: 0;
}
.knowledge-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  min-width: 0;
}
</style>
