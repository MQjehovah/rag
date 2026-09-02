<template>
  <div class="app-shell">
    <nav v-if="showNav" class="nav-bar">
      <div class="nav-brand">Notes RAG</div>
      <button
        class="nav-menu-toggle"
        type="button"
        aria-label="打开导航菜单"
        :aria-expanded="mobileMenuOpen"
        @click="mobileMenuOpen = !mobileMenuOpen"
      >
        <span></span><span></span><span></span>
      </button>
      <div class="nav-links" :class="{ 'mobile-open': mobileMenuOpen }" @click="mobileMenuOpen = false">
        <PrimaryNav :items="visibleItems" :badges="badges" />
      </div>
      <div class="nav-user" v-if="isLoggedIn">
        <UserMenu :admin-items="visibleAdminItems" />
      </div>
    </nav>
    <main class="app-main">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { ADMIN_MENU, PRIMARY_NAV } from '../navigation/config'
import { hasRole } from '../navigation/permissions'
import { featureVisible, loadFeatureFlags } from '../navigation/featureFlags'
import type { NavigationItem } from '../navigation/types'
import PrimaryNav from '../components/navigation/PrimaryNav.vue'
import UserMenu from '../components/navigation/UserMenu.vue'

const route = useRoute()
const authStore = useAuthStore()
const mobileMenuOpen = ref(false)

const showNav = computed(() => route.path !== '/login')
const isLoggedIn = computed(() => authStore.isLoggedIn)

/** 按角色过滤顶级导航。 */
const visibleItems = computed<NavigationItem[]>(() =>
  PRIMARY_NAV.filter((item) => hasRole(authStore.user, item.roles) && featureVisible(item.featureFlag))
)

const visibleAdminItems = computed<NavigationItem[]>(() =>
  ADMIN_MENU.filter((item) => hasRole(authStore.user, item.roles) && featureVisible(item.featureFlag))
)

/** 徽标数据（后续接 review_count/debt_count/source_error_count）。 */
const badges = computed<Record<string, number>>(() => ({}))

watch(() => route.fullPath, () => {
  mobileMenuOpen.value = false
})

watch(() => authStore.isLoggedIn, (loggedIn) => {
  if (loggedIn) void loadFeatureFlags()
}, { immediate: true })
</script>

<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  -webkit-font-smoothing: antialiased;
}
.app-shell {
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
  flex: 0 0 auto;
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
  min-width: 0;
}
.nav-menu-toggle {
  display: none;
  width: 38px;
  height: 38px;
  padding: 8px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
}
.nav-menu-toggle span {
  display: block;
  height: 2px;
  margin: 4px 0;
  border-radius: 999px;
  background: #cbd5e1;
}
.nav-menu-toggle:hover {
  background: rgba(255,255,255,0.08);
}
.nav-user {
  margin-left: auto;
  display: flex;
  align-items: center;
}
.app-main {
  flex: 1;
  overflow: hidden;
  min-width: 0;
}

@media (max-width: 1280px) {
  .nav-bar {
    position: relative;
    height: 56px;
    padding: 0 14px;
    gap: 12px;
  }
  .nav-menu-toggle {
    display: block;
  }
  .nav-links {
    display: none;
    position: absolute;
    top: calc(100% + 8px);
    left: 12px;
    right: 12px;
    z-index: 110;
    max-height: calc(100vh - 76px);
    overflow-y: auto;
    flex-direction: column;
    gap: 2px;
    padding: 8px;
    border: 1px solid rgba(148,163,184,0.2);
    border-radius: 12px;
    background: #1e293b;
    box-shadow: 0 16px 36px rgba(15,23,42,0.35);
  }
  .nav-links.mobile-open {
    display: flex;
  }
  .nav-user {
    margin-left: auto;
  }
}

@media (max-width: 520px) {
  .nav-brand {
    font-size: 15px;
  }
}
</style>
