/** Phase G：默认路由（旧 Card/KO/治理/审核/owner/高风险入口已退出）。 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { buildLegacyRedirectRoutes } from './legacyRedirects'

/** 显式声明 meta。 */
const routes: RouteRecordRaw[] = [
  { path: '/login', component: () => import('../views/Login.vue'), meta: { requiresAuth: false, title: '登录' } },

  // 现有页面
  { path: '/', redirect: '/assistant' },
  { path: '/assistant', component: () => import('../views/Chat.vue'), meta: { requiresAuth: true, navGroup: 'assistant', title: 'AI 问答' } },

  // Phase F/G：知识缺口（V4）
  { path: '/knowledge-gaps', component: () => import('../views/KnowledgeDebtV4.vue'), meta: { requiresAuth: true, title: '知识缺口' } },

  // J-2：删除普通用户独立 /search 正式路由。普通用户经 AI 问答与知识中心
  // 完成问答、Wiki 搜索与原始资料查找；管理员检索评测由 /admin/retrieval 提供
  // （复用 Search.vue，见下方 admin.children）。

  // P8-FE-17/18：系统工具（AdminLayout 父布局 + 二级导航）
  {
    path: '/admin',
    component: () => import('../layouts/AdminLayout.vue'),
    meta: { requiresAuth: true, requiredRoles: ['system_admin'], navGroup: 'admin', title: '系统工具' },
    children: [
      { path: '', redirect: '/admin/retrieval' },
      { path: 'retrieval', component: () => import('../views/Search.vue'), meta: { requiresAuth: true, requiredRoles: ['system_admin'], title: '检索评测' } },
      { path: 'flags', component: () => import('../views/admin/FeatureFlags.vue'), meta: { requiresAuth: true, requiredRoles: ['system_admin'], title: 'Feature Flag' } },
      { path: 'model-health', component: () => import('../views/admin/ModelHealth.vue'), meta: { requiresAuth: true, requiredRoles: ['system_admin'], title: '模型健康' } },
    ],
  },

  // P8-FE-19：数据源顶级入口（管理员 + Feature Flag）
  { path: '/sources', component: () => import('../views/Sources.vue'), meta: { requiresAuth: true, requiredRoles: ['knowledge_admin'], featureFlag: 'source_hub_enabled', navGroup: 'sources', title: '数据源' } },

  // 知识中心（父布局 + 二级导航）
  {
    path: '/knowledge',
    component: () => import('../layouts/KnowledgeLayout.vue'),
    meta: { requiresAuth: true, navGroup: 'knowledge', title: '知识中心' },
    children: [
      { path: '', redirect: '/knowledge/wiki' },
      { path: 'wiki', component: () => import('../views/Wiki.vue'), meta: { requiresAuth: true, title: 'Wiki 主题' } },
      { path: 'wiki/:pageId', component: () => import('../views/Wiki.vue'), meta: { requiresAuth: true, title: 'Wiki 主题' } },
      { path: 'documents', component: () => import('../views/Editor.vue'), meta: { requiresAuth: true, title: '原始资料' } },
      { path: 'graph', component: () => import('../views/KnowledgeGraphHub.vue'), meta: { requiresAuth: true, title: '知识图谱' } },
    ],
  },

  // 错误页
  { path: '/403', component: () => import('../views/Forbidden.vue'), meta: { requiresAuth: true, title: '无权访问' } },
  { path: '/404', component: () => import('../views/NotFound.vue'), meta: { requiresAuth: true, title: '页面不存在' } },
  { path: '/feature-disabled', component: () => import('../views/FeatureDisabled.vue'), meta: { requiresAuth: true, title: '功能未启用' } },

  // 兜底 404
  { path: '/:pathMatch(.*)*', component: () => import('../views/NotFound.vue'), meta: { requiresAuth: true, title: '页面不存在' } },
]

export const router = createRouter({
  history: createWebHistory(),
  routes: [...routes, ...buildLegacyRedirectRoutes()],
})
