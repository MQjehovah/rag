import http from './http'

/** Phase 8C 管理员“当前工作区编译任务”面板 API（复用 wiki_compile，见 docs/phase-8c-contract.md §3）。
 *
 * 仅管理员可调用（require_admin）；普通编辑者前端不渲染/不请求。
 * 类型只投影后端白名单键（见 backend/app/api/wiki_compile.py _serialize_run/_serialize_stage）：
 * 不消费 payload_json、Prompt、lease/worker、request_fingerprint、metrics 原始 JSON 等敏感字段。
 */

export type CompileRunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'superseded' | string

export interface CompileRunSummary {
  id: string
  pipeline_key: string | null
  pipeline_version: string | null
  trigger_type: string | null
  trigger_object_id: string | null
  source_sync_run_id: string | null
  workspace_id: string | null
  wiki_page_id: string | null
  status: CompileRunStatus
  current_stage: string | null
  output_revision_id: string | null
  error_summary: string | null
  safe_error_code: string | null
  safe_error_message: string | null
  attempt: number | null
  max_attempts: number | null
  cancel_requested: boolean
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
}

export interface CompileStageRun {
  id: string
  run_id: string
  stage_key: string | null
  stage_order: number | null
  status: string | null
  attempt: number | null
  retryable: boolean
  component_key: string | null
  component_version: string | null
  parent_stage_run_id: string | null
  error_code: string | null
  error_message: string | null
  safe_error_code: string | null
  safe_error_message: string | null
  metrics_summary: Record<string, unknown>
  started_at: string | null
  finished_at: string | null
  created_at: string | null
}

/** get_run 详情：run + stage 摘要（前端只用 stages 时间线，不展示 artifact payload）。 */
export interface CompileRunDetail extends CompileRunSummary {
  stages: CompileStageRun[]
}

export interface CompileRunListResult {
  total: number
  limit: number
  offset: number
  runs: CompileRunSummary[]
}

export const wikiCompileApi = {
  /** 按当前工作区过滤的编译任务列表（先过滤后 count/分页）。 */
  async listRuns(params: { workspaceId: string; status?: string; limit?: number; offset?: number }): Promise<CompileRunListResult> {
    const res = await http.get('/api/wiki-compile/runs', {
      params: {
        workspace_id: params.workspaceId,
        status: params.status,
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      },
    })
    return res.data
  },

  async getRun(runId: string): Promise<CompileRunDetail> {
    const res = await http.get(`/api/wiki-compile/runs/${runId}`)
    return res.data
  },

  async retryRun(runId: string): Promise<{ run_id: string; status: string; attempt: number; message: string }> {
    const res = await http.post(`/api/wiki-compile/runs/${runId}/retry`)
    return res.data
  },

  async cancelRun(runId: string): Promise<{ run_id: string; status: string; cancel_requested: boolean; message: string }> {
    const res = await http.post(`/api/wiki-compile/runs/${runId}/cancel`)
    return res.data
  },
}
