<template>
  <nav class="primary-nav" aria-label="主导航">
    <router-link
      v-for="item in items"
      :key="item.key"
      :to="item.route"
      class="primary-nav-item"
      active-class="active"
    >
      <span class="nav-label">{{ item.label }}</span>
      <span v-if="badgeText(item)" class="nav-badge">{{ badgeText(item) }}</span>
    </router-link>
  </nav>
</template>

<script setup lang="ts">
import type { NavigationItem } from '../../navigation/types'

const props = defineProps<{
  items: NavigationItem[]
  badges?: Record<string, number>
}>()

function badgeText(item: NavigationItem): string {
  if (!item.badge || !props.badges) return ''
  const count = props.badges[item.badge] || 0
  if (count <= 0) return ''
  return count > 99 ? '99+' : String(count)
}
</script>

<style scoped>
.primary-nav {
  display: flex;
  gap: 2px;
  min-width: 0;
}
.primary-nav-item {
  padding: 7px 16px;
  border-radius: 8px;
  text-decoration: none;
  color: #94a3b8;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.2s;
  white-space: nowrap;
  display: flex;
  align-items: center;
  gap: 6px;
  position: relative;
}
.primary-nav-item:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #e2e8f0;
}
.primary-nav-item.active {
  background: rgba(56, 189, 248, 0.15);
  color: #38bdf8;
}
.nav-badge {
  font-size: 11px;
  line-height: 1;
  background: #ef4444;
  color: #fff;
  border-radius: 999px;
  padding: 2px 6px;
}
</style>
