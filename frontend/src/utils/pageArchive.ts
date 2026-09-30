/**
 * 笔记本整本/子树导出(zip)的纯逻辑:
 * - 按页面树层级生成 zip 内的相对路径(`父/子/标题.md`);
 * - 渲染带 frontmatter(标题/更新时间)的 Markdown;
 * - 文件名净化与同名去重;并发映射 mapLimit。
 */

export interface ArchiveTreePage {
  id: string
  title?: string
  parent_id?: string | null
  position?: number
  updated_at?: string
}

export const ARCHIVE_NAME_MAX = 80
export const ARCHIVE_ERRORS_FILE = '_errors.txt'

/** 文件名段净化: 去掉路径分隔符/控制字符/保留字符, 限长。 */
export function sanitizeFileSegment(name: string): string {
  const cleaned = String(name || '')
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_')
    .replace(/\.+$/, '')
    .trim()
  return (cleaned || '无标题').slice(0, ARCHIVE_NAME_MAX)
}

/**
 * 生成 id → zip 路径 的映射:
 * - 根页面 `标题.md`;子页面 `父标题/子标题.md`(按 position 排序);
 * - 同目录同名追加 ` (2)`/` (3)`…;父级缺失时视为根页面。
 */
export function buildArchiveEntries(pages: ArchiveTreePage[]): Map<string, string> {
  const byId = new Map(pages.map(p => [p.id, p]))
  const childrenOf = new Map<string | null, ArchiveTreePage[]>()
  for (const p of pages) {
    const pid = p.parent_id && byId.has(p.parent_id) ? p.parent_id : null
    if (!childrenOf.has(pid)) childrenOf.set(pid, [])
    childrenOf.get(pid)!.push(p)
  }
  for (const list of childrenOf.values()) {
    list.sort((a, b) =>
      (a.position ?? 0) - (b.position ?? 0)
      || String(a.title || '').localeCompare(String(b.title || ''), 'zh'))
  }

  const entries = new Map<string, string>()
  const walk = (pid: string | null, prefix: string) => {
    const used = new Set<string>()
    for (const page of childrenOf.get(pid) || []) {
      const base = sanitizeFileSegment(page.title || '无标题')
      let name = base
      let n = 2
      while (used.has(name)) name = `${base} (${n++})`
      used.add(name)
      const dir = prefix ? `${prefix}/${name}` : name
      entries.set(page.id, `${dir}.md`)
      walk(page.id, dir)
    }
  }
  walk(null, '')
  return entries
}

const escapeYaml = (value: string) => value.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\r?\n/g, ' ')

/** 单页 Markdown: frontmatter(title/updated_at) + 原文。 */
export function renderArchivePage(page: { title?: string; content?: string; updated_at?: string }): string {
  const title = String(page.title || '无标题')
  const lines = ['---', `title: "${escapeYaml(title)}"`]
  if (page.updated_at) lines.push(`updated_at: "${escapeYaml(String(page.updated_at))}"`)
  lines.push('---', '', String(page.content || ''))
  return lines.join('\n')
}

/** 并发受限的 map(limit ≤ items.length);保持结果顺序, 单步异常向上抛。 */
export async function mapLimit<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length)
  const workers = Math.max(1, Math.min(limit, items.length))
  let next = 0
  await Promise.all(Array.from({ length: workers }, async () => {
    while (true) {
      const index = next++
      if (index >= items.length) return
      results[index] = await fn(items[index], index)
    }
  }))
  return results
}
