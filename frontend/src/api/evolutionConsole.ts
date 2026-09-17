import http from './http'

// 阶段 8A 只读控制台 API 契约层：每个导出函数 = 后端端点的稳定契约名；
// 统一 typedGet 完成类型化响应解包。仅 GET，无任何写操作。

export interface ConsoleStatus {
  console: 'ok' | 'disabled' | 'unavailable'
  reason?: string | null
  roots: { id: string; label: string }[]
  real_link_verified: boolean
  effect_verified: boolean
}

export interface Page<T> {
  items: T[]
  page: number
  size: number
  total: number
}

function typedGet<T>(path: string, params?: Record<string, unknown>) {
  return http.get<T>(path, { params }).then((r) => r.data)
}

export const consoleStatus = () =>
  typedGet<ConsoleStatus>('/api/evolution-console/status')

export const listExperiments = (root: string, page = 1, size = 20, q = '') =>
  typedGet<Page<Record<string, unknown>>>('/api/evolution-console/experiments', {
    root,
    page,
    size,
    q,
  })

export const experimentDetail = (experimentId: string, root: string) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-console/experiments/${encodeURIComponent(experimentId)}`,
    { root }
  )

export const experimentSkills = (experimentId: string, root: string) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-console/experiments/${encodeURIComponent(experimentId)}/skills`,
    { root }
  )

export const experimentVersions = (experimentId: string, root: string) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-console/experiments/${encodeURIComponent(experimentId)}/versions`,
    { root }
  )

export const skillContent = (versionId: string, root: string) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-console/skills/${encodeURIComponent(versionId)}/content`,
    { root }
  )

export const skillDiff = (
  versionId: string,
  baseVersionId: string,
  root: string
) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-console/skills/${encodeURIComponent(versionId)}/diff`,
    { root, base_version_id: baseVersionId }
  )

export const experimentPatterns = (experimentId: string, root: string, page = 1) =>
  typedGet<Page<Record<string, unknown>>>(
    `/api/evolution-console/experiments/${encodeURIComponent(experimentId)}/patterns`,
    { root, page, size: 20 }
  )

export const gateHistory = (experimentId: string, root: string, page = 1) =>
  typedGet<Page<Record<string, unknown>>>(
    `/api/evolution-console/experiments/${encodeURIComponent(experimentId)}/gate-history`,
    { root, page, size: 20 }
  )

export const evaluations = (experimentId: string, root: string, page = 1) =>
  typedGet<Page<Record<string, unknown>>>(
    `/api/evolution-console/experiments/${encodeURIComponent(experimentId)}/evaluations`,
    { root, page, size: 20 }
  )

export const runDetail = (runId: string, root: string) =>
  typedGet<Record<string, unknown>>(
    `/api/evolution-console/runs/${encodeURIComponent(runId)}`,
    { root }
  )

export const trajectories = (runId: string, root: string, page = 1) =>
  typedGet<Page<Record<string, unknown>>>(
    `/api/evolution-console/runs/${encodeURIComponent(runId)}/trajectories`,
    { root, page, size: 20 }
  )
