/** P8-FE-01：最终导航配置（V3 计划 3.3/3.4）。

 * 配置驱动：同一份配置驱动桌面导航、移动抽屉、面包屑。
 * 阶段 A 只建立配置与类型，不改变现有页面组件；route 先指向现有组件，
 * 阶段 B/C/D 逐步替换 route 与组件映射。
 */

import type { NavigationItem } from './types'

/** 知识中心二级导航（统一配置，供布局与顶级导航共用）。 */
export const KNOWLEDGE_SECTIONS: NavigationItem[] = [
  { key: 'wiki', label: 'Wiki 主题', route: '/knowledge/wiki', order: 1, roles: ['user'] },
  { key: 'documents', label: '原始资料', route: '/knowledge/documents', order: 2, roles: ['user'] },
  { key: 'graph', label: '知识图谱', route: '/knowledge/graph', order: 3, roles: ['user'] },
]

/** 顶级导航入口（Phase G）。 */
export const PRIMARY_NAV: NavigationItem[] = [
  { key: 'assistant', label: 'AI 问答', route: '/assistant', order: 1, roles: ['user'] },
  { key: 'knowledge', label: '知识中心', route: '/knowledge', order: 3, roles: ['user'], children: KNOWLEDGE_SECTIONS },
  { key: 'knowledge-gaps', label: '知识缺口', route: '/knowledge-gaps', order: 4, roles: ['user'] },
  { key: 'sources', label: '数据源', route: '/sources', order: 5, roles: ['knowledge_admin'], featureFlag: 'source_hub_enabled' },
]

/** 系统工具（头像菜单，不进顶级导航）。 */
export const ADMIN_MENU: NavigationItem[] = [
  { key: 'retrieval', label: '检索评测', route: '/admin/retrieval', order: 1, roles: ['system_admin'] },
  { key: 'flags', label: 'Feature Flag', route: '/admin/flags', order: 3, roles: ['system_admin'] },
  { key: 'model-health', label: '模型健康', route: '/admin/model-health', order: 4, roles: ['system_admin'] },
]

/** Phase G：旧入口已从真实路由移除，不再保留旧写页面 redirect 映射。 */
export const LEGACY_REDIRECTS: Record<string, string> = {
  '/wiki': '/knowledge/wiki',
}
