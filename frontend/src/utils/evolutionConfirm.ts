// M3 整改：real start/resume 完整确认构造与 rollback 请求级幂等 pending 状态。
// 纯函数/无 DOM，供 EvolutionConsole 与（如后续）测试复用。

export interface PreviewBudget {
  max_model_calls: number
  max_tool_calls: number
  max_seconds: number
}

export interface StartPreview {
  model_mode?: string
  dataset_version?: string
  max_iterations?: number
  budget?: PreviewBudget | null
  config_fingerprint?: string | null
  reviewer_fingerprint?: string | null
  roles?: { role?: string; model?: string; endpoint_host?: string;
            max_output_tokens?: number | null; retries?: number }[]
  reviewer?: { model_id?: string; endpoint_host?: string;
               max_output_tokens?: number | null } | null
}

export interface RealStartConfirm {
  explicit_confirm: boolean
  confirm_dataset_version: string
  confirm_max_iterations: number
  confirm_max_model_calls: number
  confirm_max_tool_calls: number
  confirm_max_seconds: number
  confirm_config_fingerprint: string
  confirm_reviewer_fingerprint: string | null
}

/** 由服务端 start-preview 响应构造完整确认体；缺失/null/非法 → 抛错（fail-closed）。 */
export function buildRealStartConfirm(preview: StartPreview): RealStartConfirm {
  const ds = preview.dataset_version
  const iters = preview.max_iterations
  const b = preview.budget
  const fp = preview.config_fingerprint
  if (!ds || typeof iters !== 'number' || !Number.isFinite(iters)
      || !Number.isInteger(iters) || iters < 1) {
    throw new Error('预览缺少 dataset_version/max_iterations（缺失/null/非整数/越界，拒绝提交 start/resume）')
  }
  if (!b || typeof b.max_model_calls !== 'number' || !Number.isFinite(b.max_model_calls)
      || typeof b.max_tool_calls !== 'number' || !Number.isFinite(b.max_tool_calls)
      || typeof b.max_seconds !== 'number' || !Number.isFinite(b.max_seconds)
      || b.max_model_calls < 1 || b.max_tool_calls < 1 || b.max_seconds < 1) {
    throw new Error('预览缺少完整预算字段（max_model_calls/max_tool_calls/max_seconds）')
  }
  if (!fp) {
    throw new Error('预览缺少配置指纹（real 配置未就绪，拒绝提交）')
  }
  return {
    explicit_confirm: true,
    confirm_dataset_version: ds,
    confirm_max_iterations: iters,
    confirm_max_model_calls: b.max_model_calls,
    confirm_max_tool_calls: b.max_tool_calls,
    confirm_max_seconds: b.max_seconds,
    confirm_config_fingerprint: fp,
    confirm_reviewer_fingerprint: preview.reviewer_fingerprint ?? null,
  }
}

export interface PendingRollback {
  workspace_id: string
  expected_rev: number | null
  expected_set_hash: string | null
  idempotency_key: string
}

export type RollbackErrorKind =
  | 'indeterminate'   // 无响应/网络/5xx：可同键重试
  | 'conflict'        // 409：作废并重新预览
  | 'idem_conflict'   // 422：作废并重新发起
  | 'rejected'        // 其它确定性错误：作废并重新发起

export function classifyRollbackError(err: unknown): RollbackErrorKind {
  const e = err as { response?: { status?: number } }
  const status = e?.response?.status
  if (status == null) return 'indeterminate'          // 网络断开/timeout/无 HTTP 响应
  if (status >= 500) return 'indeterminate'           // 含故障注入 502
  if (status === 409) return 'conflict'
  if (status === 422) return 'idem_conflict'
  return 'rejected'
}

export function newIdempotencyKey(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch {
    /* ignore */
  }
  return 'k-' + Date.now() + '-' + Math.random().toString(36).slice(2)
}

/** 新回退操作：刷新后由当前绑定创建新 pending（含新键）。 */
export function createPendingRollback(
  workspaceId: string,
  expectedRev: number | null,
  expectedSetHash: string | null
): PendingRollback {
  return {
    workspace_id: workspaceId,
    expected_rev: expectedRev,
    expected_set_hash: expectedSetHash,
    idempotency_key: newIdempotencyKey(),
  }
}

/** 同键重试必须逐字复用原 pending，不得刷新绑定/生成新键。 */
export function retryBody(p: PendingRollback): Record<string, unknown> {
  return {
    workspace_id: p.workspace_id,
    expected_rev: p.expected_rev ?? null,
    expected_set_hash: p.expected_set_hash ?? null,
    idempotency_key: p.idempotency_key,
  }
}
