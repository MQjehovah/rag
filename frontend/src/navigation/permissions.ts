/** P8-FE-01：角色判定纯函数（不信任前端 JWT，以 /api/auth/me 返回为准）。 */

import type { UserRole } from './types'

export interface CurrentUser {
  groups: string[]
  is_admin?: boolean
}

/** 从后端返回的 groups 推导角色。 */
export function resolveRoles(user: CurrentUser | null | undefined): UserRole[] {
  if (!user) return []
  const roles: UserRole[] = ['user']
  // 知识管理员：本地管理员组或 LDAP 管理员组，或后端显式 is_admin
  if (user.is_admin || (user.groups || []).includes('__local_admin__')) {
    roles.push('knowledge_admin', 'system_admin')
  }
  return roles
}

/** 判断用户是否满足某导航/路由要求的角色。 */
export function hasRole(user: CurrentUser | null | undefined, required?: UserRole[]): boolean {
  if (!required || required.length === 0) return true
  const roles = resolveRoles(user)
  return required.some((r) => roles.includes(r))
}

/** 判断是否为管理员（知识管理员或系统管理员）。 */
export function isAdmin(user: CurrentUser | null | undefined): boolean {
  return hasRole(user, ['knowledge_admin'])
}
