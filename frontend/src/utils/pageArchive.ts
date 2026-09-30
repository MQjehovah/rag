/**
 * 笔记本整本/子树导出(zip)的纯逻辑:
 * - 按页面树层级生成 zip 内的相对路径(`父/子/标题.md`);
 * - 渲染带 frontmatter(标题/更新时间)的 Markdown;
 * - 文件名净化与同名去重;并发映射 mapLimit;
 * - 附件引用提取/文件名还原/去重清单/链接改写(纯函数, zip 附带附件用)。
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
      // 文件名(`name.md`)与目录名(`name`)都纳入去重集合, 防止 `guide.md` 文件与 `guide/` 目录互相占用同名路径
      while (used.has(name) || used.has(`${name}.md`)) name = `${base} (${n++})`
      used.add(name)
      used.add(`${name}.md`)
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

// ---------------- 附件(zip 附带) ----------------

export interface AttachmentRef {
  /** 正文中出现的原始地址(可能带签名查询串), 供链接改写精确匹配 */
  url: string
  /** 供 zip 使用的文件名(已还原 hash12 前缀, 尚未做重名去重) */
  name: string
}

export interface ArchiveAttachment {
  /** 稳定去重键(路径, 去域名/查询) */
  key: string
  /** 用于下载的原始地址 */
  url: string
  /** zip 内文件名(不含 attachments/ 前缀, 已重名去重) */
  name: string
  /** zip 内相对路径 `attachments/<name>` */
  path: string
}

/** 正文中候选 URL: 绝对地址或根相对路径(排除空白/引号/括号). */
const ATTACHMENT_URL_RE = /(?:https?:\/\/|\/)[^\s"'<>()\\]+/g

/** HTML 属性里的实体还原(serialize 时 URL 中的 &/" 会被转义). */
const unescapeHtmlAttr = (text: string): string => String(text || '')
  .replace(/&quot;/g, '"')
  .replace(/&lt;/g, '<')
  .replace(/&gt;/g, '>')
  .replace(/&amp;/g, '&')

/** 去掉 URL 结尾误吞的标点(中文标点/句读), 保留其余部分. */
const trimUrlTail = (raw: string): string => raw.replace(/[.,;:!?。，；：！？、…]+$/u, '')

/** `<hash12>-原名` → `原名`(旧 uuid12/新 sha256-12 均为 12 位小写十六进制); 其余原样返回. */
export function restoreAttachmentName(fileName: string): string {
  const name = String(fileName || '')
  const restored = name.replace(/^[0-9a-f]{12}-/, '')
  return restored || name
}

/**
 * 判定并解析附件地址(纯函数):
 * - 本地 `/api/upload/attachments/...`(可带 BASE_URL 前缀与签名查询);
 * - MinIO 对象路径 `.../attachments/<yyyymmdd>/<hash12>-<原名>`;
 * 其它地址(data:/blob:/普通图片/页面链接)返回 null。
 */
export function parseAttachmentUrl(raw: string): AttachmentRef | null {
  const text = String(raw || '').trim()
  if (!text || /^(data|blob):/i.test(text)) return null
  let u: URL
  try {
    u = new URL(text, 'http://attachment.invalid')
  } catch {
    return null
  }
  const path = u.pathname
  const isLocalApi = /\/api\/upload\/attachments\//.test(path)
  const isMinio = /\/attachments\/\d{8}\//.test(path)
  if (!isLocalApi && !isMinio) return null
  const file = decodeURIComponent(path.slice(path.lastIndexOf('/') + 1))
  if (!file) return null
  return { url: text, name: restoreAttachmentName(file) }
}

/** 稳定去重键: 路径(去域名/查询), 使同一文件的不同签名/前缀写法只导出一次; 非法返回空串. */
export function attachmentArchiveKey(raw: string): string {
  try {
    return new URL(String(raw || ''), 'http://attachment.invalid').pathname
  } catch {
    return ''
  }
}

/**
 * 从页面 Markdown/HTML 中提取附件引用:
 * - `<div data-attachment data-url data-name>`(文件名优先用 data-name, 比 URL 末段可靠);
 * - 正文中其它位置的附件地址(裸路径 / Markdown 链接 / MinIO 直链);
 * 按路径去重, 保留首次出现的写法。
 */
export function extractAttachmentRefs(content: string): AttachmentRef[] {
  const out = new Map<string, AttachmentRef>()
  const text = String(content || '')
  const add = (url: string, name?: string) => {
    const ref = parseAttachmentUrl(url)
    if (!ref) return
    const key = attachmentArchiveKey(ref.url)
    if (!key || out.has(key)) return
    const preferred = String(name || '').trim()
    out.set(key, { url: ref.url, name: preferred || ref.name })
  }
  // 1) 附件卡片 div: data-url 为准, data-name 提供原始文件名
  const tagRe = /<div\b[^>]*\bdata-attachment\b[^>]*>/gi
  let m: RegExpExecArray | null
  while ((m = tagRe.exec(text))) {
    const tag = m[0]
    const url = (/\bdata-url\s*=\s*"([^"]*)"/i.exec(tag)?.[1]
      || /\bdata-url\s*=\s*'([^']*)'/i.exec(tag)?.[1]
      || '').trim()
    const name = /\bdata-name\s*=\s*"([^"]*)"/i.exec(tag)?.[1]
      || /\bdata-name\s*=\s*'([^']*)'/i.exec(tag)?.[1]
      || ''
    if (url) add(unescapeHtmlAttr(url), unescapeHtmlAttr(name))
  }
  // 2) 全文其余附件地址(裸路径 / Markdown 链接 / MinIO 直链)
  ATTACHMENT_URL_RE.lastIndex = 0
  while ((m = ATTACHMENT_URL_RE.exec(text))) {
    add(unescapeHtmlAttr(trimUrlTail(m[0])))
  }
  return Array.from(out.values())
}

/**
 * 附件导出清单: 按输入顺序为每个去重键分配 zip 内文件名;
 * 净化/去 hash 前缀后同名追加 ` (2)`/` (3)`…(大小写不敏感, 兼容 Windows/macOS 解压)。
 */
export function buildArchiveAttachments(refs: AttachmentRef[]): ArchiveAttachment[] {
  const out: ArchiveAttachment[] = []
  const seen = new Set<string>()
  const used = new Set<string>()
  for (const ref of refs) {
    const key = attachmentArchiveKey(ref.url)
    if (!key || seen.has(key)) continue
    seen.add(key)
    const base = sanitizeFileSegment(ref.name || restoreAttachmentName(ref.url) || '附件')
    let name = base
    let n = 2
    while (used.has(name.toLowerCase())) name = `${base} (${n++})`
    used.add(name.toLowerCase())
    out.push({ key, url: ref.url, name, path: `attachments/${name}` })
  }
  return out
}

/**
 * 把正文中的附件地址改写为 zip 内相对路径 `attachments/<file>`;
 * 只替换命中清单的地址(按路径匹配, 覆盖 data-url/href/裸链接), 未命中的保持原样。
 */
export function rewriteAttachmentLinks(content: string, attachments: ArchiveAttachment[]): string {
  if (!attachments.length) return String(content || '')
  const byKey = new Map(attachments.map(a => [a.key, a.path]))
  return String(content || '').replace(ATTACHMENT_URL_RE, (raw) => {
    const url = trimUrlTail(raw)
    const target = byKey.get(attachmentArchiveKey(url))
    return target ? target + raw.slice(url.length) : raw
  })
}
