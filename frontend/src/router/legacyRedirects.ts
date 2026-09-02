/** Phase G/J-2：旧路由重定向。

 * 旧治理/审核/Card/KO 页面已从真实路由移除，直接输入旧 URL 也进入 404。
 * 仅保留仍存在的安全重定向（/wiki → 知识中心，/notes → 原始资料）。
 *
 * J-2：/search 不再是普通用户正式路由，也不再出现在 redirect 中——直接输入
 * /search 进入 404（普通用户搜索能力由知识中心的 Wiki 主题搜索与原始资料搜索
 * 提供）。管理员检索评测由 /admin/retrieval 提供，不依赖旧 /search。
 */

import type { RouteRecordRaw } from 'vue-router'

const ENABLED_REDIRECTS: Record<string, string> = {
  '/wiki': '/knowledge/wiki',
  '/notes': '/knowledge/documents',
}

/** 生成旧路由 redirect 记录。 */
export function buildLegacyRedirectRoutes(): RouteRecordRaw[] {
  return Object.entries(ENABLED_REDIRECTS).map(([path, redirect]) => ({
    path,
    redirect,
  }))
}
