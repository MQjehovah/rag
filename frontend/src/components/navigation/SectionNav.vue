<template>
  <nav class="section-nav" aria-label="二级导航">
    <router-link
      v-for="item in items"
      :key="item.key"
      :to="item.route"
      class="section-nav-item"
      active-class="active"
    >
      {{ item.label }}
    </router-link>
  </nav>
</template>

<script setup lang="ts">
import type { NavigationItem } from '../../navigation/types'

defineProps<{
  items: NavigationItem[]
}>()
</script>

<style scoped>
.section-nav {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid #e5e7eb;
  padding-bottom: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.section-nav::-webkit-scrollbar { display: none; }
.section-nav-item {
  padding: 8px 16px;
  border-radius: 8px 8px 0 0;
  text-decoration: none;
  color: #6b7280;
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  border-bottom: 2px solid transparent;
  transition: all 0.2s;
}
.section-nav-item:hover { color: #374151; }
.section-nav-item.active {
  color: #2563eb;
  border-bottom-color: #2563eb;
}
</style>
