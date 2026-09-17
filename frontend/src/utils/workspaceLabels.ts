/** Wiki 工作区只读显示标签（无 DOM）。option value 始终使用完整 workspace.id。 */

export interface WorkspaceLabelInput {
  id: string
  name?: string | null
  display_name?: string | null
  status?: string | null
}

export function workspaceBaseLabel(workspace: WorkspaceLabelInput | null | undefined): string {
  if (!workspace) return ''
  const display = String(workspace.display_name ?? '').trim()
  if (display) return display
  const name = String(workspace.name ?? '').trim()
  if (name) return name
  return String(workspace.id || '')
}

export function shortWorkspaceId(id: string | null | undefined): string {
  const value = String(id || '')
  return value.length <= 8 ? value : value.slice(0, 8)
}

function identityKey(label: string): string {
  return label.trim().toLocaleLowerCase()
}

/**
 * 按输入顺序为每个工作区生成 UI 标签。
 * 重名（trim + locale 小写）追加稳定短 ID；archived 文案不参与身份计算。
 * 不修改入参对象。
 */
export function buildWorkspaceOptionLabels(
  workspaces: ReadonlyArray<WorkspaceLabelInput> | null | undefined,
): Map<string, string> {
  const list = Array.from(workspaces || [])
  const bases = list.map((ws) => ({ ws, base: workspaceBaseLabel(ws) }))
  const counts = new Map<string, number>()
  for (const { base } of bases) {
    const key = identityKey(base)
    counts.set(key, (counts.get(key) || 0) + 1)
  }
  const result = new Map<string, string>()
  for (const { ws, base } of bases) {
    const duplicate = (counts.get(identityKey(base)) || 0) > 1
    let label = duplicate ? `${base} · ${shortWorkspaceId(ws.id)}` : base
    if (ws.status === 'archived') {
      label = `${label}（已归档）`
    }
    result.set(ws.id, label)
  }
  return result
}
