/** P8-FE-01：导航与路由统一类型定义（V3 计划 3.5/3.6）。 */

export type UserRole = 'user' | 'knowledge_admin' | 'system_admin'

export type NavGroup = 'assistant' | 'knowledge' | 'governance' | 'sources' | 'admin'

export interface NavigationItem {
  key: string
  label: string
  route: string
  order: number
  roles?: UserRole[]
  featureFlag?: string
  badge?: 'review_count' | 'debt_count' | 'source_error_count'
  children?: NavigationItem[]
}

export interface RouteMeta {
  requiresAuth: boolean
  requiredRoles?: UserRole[]
  featureFlag?: string
  navGroup?: NavGroup
  title: string
}
