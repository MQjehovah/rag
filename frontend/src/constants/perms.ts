// 与后端权限目录同步维护: backend/app/core/permissions.py (PERMISSION_CATALOG)
export const PERM = {
  sources: 'sources.manage',
  pipeline: 'pipeline.manage',
  embedding: 'embedding.manage',
  graph: 'graph.manage',
  wiki: 'wiki.admin',
  notebook: 'notebook.manage',
  page: 'page.manage',
  user: 'user.manage',
  role: 'role.manage',
  group: 'group.manage',
} as const

export type Permission = (typeof PERM)[keyof typeof PERM]
export type PermissionKey = Permission | '*'
