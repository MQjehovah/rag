/** 离线草稿: PUT 失败时把最新内容备份到 localStorage, 重新打开页面时提示恢复。
 *  key 按用户隔离(`rag-draft:{uid}:{pageId}`), 不从全局 key 回退。 */

export interface PageDraft {
  /** 最新 Markdown 内容 */
  content: string
  title: string
  /** 备份时间(epoch ms) */
  savedAt: number
  /** 备份时服务端页面的 updated_at, 用于判断期间是否被其它端保存过 */
  baseUpdatedAt: string
}

export const draftKey = (uid: string, pageId: string): string => `rag-draft:${uid || 'anon'}:${pageId}`

export function saveDraft(storage: Storage, uid: string, pageId: string, draft: PageDraft): boolean {
  if (!pageId) return false
  try {
    storage.setItem(draftKey(uid, pageId), JSON.stringify(draft))
    return true
  } catch {
    return false
  }
}

export function loadDraft(storage: Storage, uid: string, pageId: string): PageDraft | null {
  if (!pageId) return null
  try {
    const raw = storage.getItem(draftKey(uid, pageId))
    if (!raw) return null
    const data = JSON.parse(raw)
    if (!data || typeof data.content !== 'string') return null
    return {
      content: data.content,
      title: typeof data.title === 'string' ? data.title : '',
      savedAt: Number(data.savedAt) || 0,
      baseUpdatedAt: typeof data.baseUpdatedAt === 'string' ? data.baseUpdatedAt : '',
    }
  } catch {
    return null
  }
}

export function clearDraft(storage: Storage, uid: string, pageId: string): void {
  if (!pageId) return
  try {
    storage.removeItem(draftKey(uid, pageId))
  } catch { /* ignore */ }
}

/** 解析服务端时间戳; 后端存的是无时区 naive 时间, 交给 Date.parse 按本地时区解释。 */
export function parseServerTimestamp(value: string): number {
  if (!value) return NaN
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : NaN
}

/**
 * 是否提示恢复草稿:
 * - 草稿与服务端内容相同 → 无需恢复(调用方清理);
 * - 草稿记录的基准 updated_at 与服务端一致 → 期间无人保存, 草稿最新;
 * - 否则回退比较时间戳(草稿晚于服务端更新时间);
 * - 时间戳无法解析时保守提示(宁可让用户选择)。
 */
export function shouldOfferDraft(draft: PageDraft | null, serverContent: string, serverUpdatedAt: string): boolean {
  if (!draft) return false
  if (draft.content === (serverContent ?? '')) return false
  if (draft.baseUpdatedAt && serverUpdatedAt) {
    if (draft.baseUpdatedAt === serverUpdatedAt) return true
  }
  const server = parseServerTimestamp(serverUpdatedAt)
  if (!Number.isFinite(server)) return true
  return draft.savedAt > server
}
