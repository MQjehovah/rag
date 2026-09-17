import http from './http'

// 阶段 8C/8D：演化运行控制与业务晋升/回退（管理员写操作）。

export interface MetaInfo {
  datasets: {
    dataset_version: string
    grader_version: string | null
    grader_ready: boolean
    grader_purpose: string
    grader_description: string
  }[]
  grader_note: string
  real_start_enabled?: boolean
  real_link_verified: boolean
  effect_verified: boolean
}

export interface RunControl {
  run_id: string
  status: string
  stop_reason: string | null
  model_mode: string
  pause_requested?: boolean
  cancel_requested?: boolean
  [k: string]: unknown
}

function post<T>(path: string, body: Record<string, unknown>) {
  return http.post<T>(path, body).then((r) => r.data)
}
function typedGet<T>(path: string, params?: Record<string, unknown>) {
  return http.get<T>(path, { params }).then((r) => r.data)
}

export const adminMeta = () => typedGet<MetaInfo>('/api/evolution-admin/meta')

export const createExperiment = (body: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/evolution-admin/experiments', body)

export const controlRun = (
  runId: string,
  action: 'start' | 'resume' | 'pause' | 'cancel',
  root: string,
  confirm?: Record<string, unknown>
) =>
  post<Record<string, unknown>>(`/api/evolution-admin/runs/${runId}/${action}`, {
    root,
    ...(confirm || {}),
  })

export const startPreview = (runId: string, root: string) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-admin/runs/${encodeURIComponent(runId)}/start-preview`,
    { root }
  )

export const promotionPreview = (
  experimentId: string,
  root: string,
  workspaceId: string
) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-admin/experiments/${encodeURIComponent(experimentId)}/promotion/preview`,
    { root, workspace_id: workspaceId }
  )

export const businessPromote = (body: Record<string, unknown>) =>
  post<Record<string, unknown>>('/api/evolution-admin/business/promote', body)

export const businessRollback = (
  workspaceId: string,
  opts?: {
    expected_rev?: number | null
    expected_set_hash?: string | null
    idempotency_key?: string | null
  }
) =>
  post<Record<string, unknown>>('/api/evolution-admin/business/rollback', {
    workspace_id: workspaceId,
    expected_rev: opts?.expected_rev ?? null,
    expected_set_hash: opts?.expected_set_hash ?? null,
    idempotency_key: opts?.idempotency_key ?? null,
  })

export const businessState = (workspaceId: string) =>
  typedGet<Record<string, unknown>>('/api/evolution-admin/business/state', {
    workspace_id: workspaceId,
  })
