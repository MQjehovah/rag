import http from './http'

/** Phase 8A：Wiki 工作区浏览 API（只读契约）。
 *
 * GET /api/wiki-workspaces —— 当前用户可见的工作区列表。
 *   普通用户服务端已裁剪：不含 key / acl_scope / scope_id / created_by；
 *   此处类型只投影浏览所需字段，前端不展示 ACL 等敏感信息。
 * GET /api/wiki-workspaces/{workspace_id}/notebooks —— 管理员只读，查看
 *   当前工作区 active 绑定的 Notebook 摘要（不含源路径/凭据/ACL/连接配置）。
 */

export interface WikiWorkspaceSummary {
  id: string
  name: string
  display_name?: string | null
  description?: string | null
  status: string
  created_at?: string | null
  updated_at?: string | null
}

export interface NotebookBindingSummary {
  binding_id: string
  notebook_id: string
  notebook_name: string
  status: string
  created_at?: string | null
  updated_at?: string | null
}

export interface WorkspaceNotebooksResult {
  workspace_id: string
  workspace_name: string
  bindings: NotebookBindingSummary[]
}

export interface WorkspaceListResult {
  workspaces: WikiWorkspaceSummary[]
}

export const wikiWorkspacesApi = {
  /** 当前用户可见的工作区（普通用户服务端裁剪敏感字段；admin 全部）。 */
  async list(): Promise<WikiWorkspaceSummary[]> {
    const res = await http.get<WorkspaceListResult>('/api/wiki-workspaces')
    return res.data.workspaces || []
  },

  /** 管理员查看某工作区 active 绑定的 Notebook 摘要（403：非管理员）。 */
  async listNotebooks(workspaceId: string): Promise<WorkspaceNotebooksResult> {
    const res = await http.get<WorkspaceNotebooksResult>(`/api/wiki-workspaces/${workspaceId}/notebooks`)
    return res.data
  },
}
