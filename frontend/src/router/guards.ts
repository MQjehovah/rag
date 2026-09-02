/** P8-FE-03：路由守卫（V3 计划 3.6）。

 * 执行顺序：
 * 1. 恢复 token（auth store 已从 localStorage 初始化）
 * 2. token 存在但用户为空 → fetchMe()
 * 3. 未认证 → /login + 保存 redirect
 * 4. 校验角色 → /403
 * 5. 校验 Feature Flag → /feature-disabled
 * 6. 更新页面标题
 */

import type { Router } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { hasRole } from '../navigation/permissions'
import { clearFeatureFlags, loadFeatureFlags } from '../navigation/featureFlags'

/** 测试/外部可刷新 flag 缓存。 */
export function clearFlagCache() {
  clearFeatureFlags()
}

export function installGuards(router: Router, pinia: any) {
  router.beforeEach(async (to) => {
    const meta = to.meta as any
    // 公开页面直接放行
    if (meta.requiresAuth === false) return true

    const authStore = useAuthStore(pinia)

    // 未登录 → /login，保存 redirect
    if (!authStore.isLoggedIn) {
      return { path: '/login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : {} }
    }

    // token 存在但用户未加载 → fetchMe
    if (!authStore.user) {
      await authStore.fetchMe()
    }
    if (!authStore.isLoggedIn || !authStore.user) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }

    // 角色校验
    if (meta.requiredRoles && !hasRole(authStore.user, meta.requiredRoles)) {
      return { path: '/403' }
    }

    // Feature Flag 校验
    if (meta.featureFlag) {
      const flags = await loadFeatureFlags()
      if (!flags[meta.featureFlag]) {
        return { path: '/feature-disabled' }
      }
    }

    // 页面标题
    if (meta.title) {
      document.title = `${meta.title} · Notes RAG`
    }
    return true
  })
}
