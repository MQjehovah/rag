import http from './http'

export interface SourceConnection {
  id: string
  connector_key: string
  name: string
  enabled: boolean
  config_json: string
  secret_ref: boolean
  target_notebook_id: string | null
  last_success_at: string | null
  last_error_at: string | null
}

export interface SourceTargetNotebook {
  id: string
  name: string
  group_id: string | null
  /** J-1：Notebook 完整可访问组集合（主权限组 + notebook_groups 额外授权组）。 */
  groups?: string[]
  /** J-1：只读可见范围标签（全公司 / 组：… / 仅管理员 / 未确定）。 */
  scope_label?: string
}

export interface AccessScope {
  id: string
  name: string
  type: 'local' | 'ldap'
}

export interface SetupBlocker {
  code: string
  message: string
}

export interface ConnectorSetupStatus {
  connector_key: string
  name: string
  enabled: boolean
  configured: boolean
  has_connection: boolean
  connection_id: string | null
  target_notebook_id: string | null
  setup_blockers: SetupBlocker[]
  supported_modes: string[]
}

export interface SourceSyncRun {
  id: string
  connection_id: string
  mode: string
  status: string
  stage: string | null
  progress: string | null
  discovered_count: number
  created_count: number
  updated_count: number
  unchanged_count: number
  deleted_count: number
  failed_count: number
  cancel_requested: boolean
  llm_degraded: boolean
  degraded_reason: string | null
  heartbeat_at: string | null
  started_at: string | null
  finished_at: string | null
  error_summary: string | null
}

export interface SourceItem {
  id: string
  connection_id: string
  external_id: string
  state: string
  page_id: string | null
  content_hash: string | null
  last_synced_at: string | null
  last_error: string | null
}

export interface SourceSyncError {
  external_id: string | null
  stage: string | null
  error_code: string | null
  error_message: string | null
  retryable: boolean
}

export interface SyncOptions {
  mode?: string
  pilot_limit?: number | null
  selected_space_ids?: string[]
  retry_failed_only?: boolean
}

export interface FolderMapping {
  id: string
  space_id: string | null
  folder_path: string
  notebook_id: string
  notebook_name: string
  created_at: string | null
}

/** 统一数据源路径映射（P38）。 */
export interface PathMapping {
  id: string
  connection_id: string
  path_namespace: string | null
  folder_path: string
  notebook_id: string
  notebook_name: string
  scope_label?: string
  created_at: string | null
}

/** 连接路径能力状态。 */
export interface PathCapability {
  paths: { path_namespace: string; folder_path: string }[]
  capability: 'path' | 'connection_level' | 'none'
  source: string
}

export const sourceApi = {
  async list(): Promise<{
    connectors: string[]
    connector_setup: Record<string, ConnectorSetupStatus>
    connections: SourceConnection[]
    target_notebooks: SourceTargetNotebook[]
    source_hub_enabled: boolean
  }> {
    const res = await http.get('/api/sources')
    return res.data
  },

  async create(payload: {
    connector_key: string
    name: string
    config_json?: object
    target_notebook_id?: string
    default_acl_json?: object
  }): Promise<SourceConnection> {
    const res = await http.post('/api/sources/connections', payload)
    return res.data
  },

  async get(id: string): Promise<SourceConnection> {
    const res = await http.get(`/api/sources/connections/${id}`)
    return res.data
  },

  async update(id: string, payload: Partial<{ name: string; enabled: boolean; config_json: object; default_acl_json: object; target_notebook_id: string | null }>): Promise<SourceConnection> {
    const res = await http.patch(`/api/sources/connections/${id}`, payload)
    return res.data
  },

  async test(id: string): Promise<{ ok: boolean; message: string; error_code: string | null }> {
    const res = await http.post(`/api/sources/connections/${id}/test`)
    return res.data
  },

  async sync(id: string, options: SyncOptions = {}): Promise<{ run_id: string; status: string; mode: string }> {
    const res = await http.post(`/api/sources/connections/${id}/sync`, options)
    return res.data
  },

  async cancelRun(runId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/sources/runs/${runId}/cancel`)
    return res.data
  },

  async retryRun(runId: string): Promise<{ run_id: string; status: string; retry_count: number }> {
    const res = await http.post(`/api/sources/runs/${runId}/retry`)
    return res.data
  },

  async runs(connectionId?: string): Promise<SourceSyncRun[]> {
    const res = await http.get('/api/sources/runs', { params: { connection_id: connectionId } })
    return res.data.runs
  },

  async run(runId: string): Promise<SourceSyncRun> {
    const res = await http.get(`/api/sources/runs/${runId}`)
    return res.data
  },

  async runErrors(runId: string): Promise<SourceSyncError[]> {
    const res = await http.get(`/api/sources/runs/${runId}/errors`)
    return res.data.errors
  },

  async items(connectionId?: string): Promise<SourceItem[]> {
    const res = await http.get('/api/sources/items', { params: { connection_id: connectionId } })
    return res.data.items
  },

  async accessScopes(): Promise<AccessScope[]> {
    const res = await http.get('/api/notebooks/access-scopes')
    return res.data.scopes || []
  },

  async updateAccessScope(notebookId: string, groupId: string): Promise<{ id: string; name: string; group_id: string | null }> {
    const res = await http.patch(`/api/notebooks/${notebookId}/access-scope`, { group_id: groupId })
    return res.data
  },

  // J-1 遗留修复：Notebook 多组授权整体替换（管理员）。
  async replaceNotebookGroups(notebookId: string, groupNames: string[]): Promise<{ id: string; name: string; group_id: string | null; groups: string[] }> {
    const res = await http.put(`/api/notebooks/${notebookId}/groups`, { group_names: groupNames })
    return res.data
  },

  // J-1 最终返工：Notebook 权限原子更新（单事务）。
  async updateNotebookPermissions(notebookId: string, payload: { scope_type: 'company' | 'admin' | 'groups'; group_names: string[] }): Promise<{ id: string; name: string; scope_type: string; group_id: string | null; groups: string[] }> {
    const res = await http.put(`/api/notebooks/${notebookId}/permissions`, payload)
    return res.data
  },

  // J-1 遗留修复：已发现的钉钉文件夹列表（供映射选择）。
  async listDiscoveredFolders(): Promise<{ space_id: string; folder_path: string }[]> {
    const res = await http.get('/api/sources/dingtalk/folder-mappings/folders')
    return res.data.folders || []
  },

  // J-1：钉钉文件夹 → Notebook 权限映射
  async listFolderMappings(): Promise<FolderMapping[]> {
    const res = await http.get('/api/sources/dingtalk/folder-mappings')
    return res.data.mappings || []
  },

  async createFolderMapping(payload: { space_id?: string; folder_path: string; notebook_id: string }): Promise<FolderMapping> {
    const res = await http.post('/api/sources/dingtalk/folder-mappings', payload)
    return res.data
  },

  async updateFolderMapping(id: string, payload: { space_id?: string; folder_path: string; notebook_id: string }): Promise<FolderMapping> {
    const res = await http.put(`/api/sources/dingtalk/folder-mappings/${id}`, payload)
    return res.data
  },

  async deleteFolderMapping(id: string): Promise<{ message: string }> {
    const res = await http.delete(`/api/sources/dingtalk/folder-mappings/${id}`)
    return res.data
  },

  // P38：统一数据源路径权限映射。
  async listPathMappings(connectionId?: string): Promise<PathMapping[]> {
    const res = await http.get('/api/sources/path-mappings', { params: { connection_id: connectionId } })
    return res.data.mappings || []
  },

  async listDiscoveredPaths(connectionId: string): Promise<PathCapability> {
    const res = await http.get('/api/sources/path-mappings/paths', { params: { connection_id: connectionId } })
    return res.data
  },

  async createPathMapping(payload: { connection_id: string; path_namespace?: string; folder_path: string; notebook_id: string }): Promise<PathMapping> {
    const res = await http.post('/api/sources/path-mappings', payload)
    return res.data
  },

  async updatePathMapping(id: string, payload: { connection_id: string; path_namespace?: string; folder_path: string; notebook_id: string }): Promise<PathMapping> {
    const res = await http.put(`/api/sources/path-mappings/${id}`, payload)
    return res.data
  },

  async deletePathMapping(id: string): Promise<{ message: string }> {
    const res = await http.delete(`/api/sources/path-mappings/${id}`)
    return res.data
  },
}
