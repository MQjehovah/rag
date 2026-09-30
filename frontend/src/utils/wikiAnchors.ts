/**
 * Wiki-link 锚点工具(纯函数): 标题 slug / 标题提取 / [[页面#锚点]] 解析 / 悬浮预览摘要。
 *
 * 编辑器(TipTap 标题 id 装饰)与阅读渲染(markdown-it 标题 id 插件)共用同一套
 * slug 规则, 保证「补全插入的锚点」与「渲染出的标题 id」一致, 可稳定滚动定位。
 */

export interface HeadingAnchor {
  level: number
  text: string
  slug: string
}

export interface WikiTarget {
  title: string
  anchor: string
}

export interface WikiMentionQuery {
  /** 去掉结尾 ]] 后的查询串 */
  raw: string
  /** 查询(或光标所在链接)是否已闭合 */
  complete: boolean
  pagePart: string
  /** null 表示查询里没有 #, 即页面补全阶段 */
  anchorPart: string | null
}

/**
 * 标题文本 → 稳定 slug: 小写、空白转连字符、剔除标点;
 * 中文/字母/数字/连字符/下划线保留(GitHub 风格附近似)。
 */
export function slugifyHeading(text: string): string {
  const base = String(text || '')
    .trim()
    .toLowerCase()
    .replace(/[\s\u3000]+/g, '-')
    .replace(/[^\p{L}\p{N}\-_]/gu, '')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '')
  return base || 'section'
}

/** slug 分配器: 重复标题按出现顺序追加 -1/-2(与渲染侧保持一致) */
export function createHeadingSlugger(): (text: string) => string {
  const seen = new Map<string, number>()
  return (text: string) => {
    const base = slugifyHeading(text)
    const n = seen.get(base) || 0
    seen.set(base, n + 1)
    return n === 0 ? base : `${base}-${n}`
  }
}

/** 去掉行内 Markdown 标记用于 slug/摘要(链接保留文本、HTML 标签剥离) */
function stripInlineMarkdown(text: string): string {
  return String(text || '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/<[^>]*>/g, '')
    .replace(/[*_~`]/g, '')
    .trim()
}

interface ScannedHeading extends HeadingAnchor {
  /** 标题所在行号(0 基) */
  line: number
}

function isFenceStart(line: string): string {
  const m = /^\s{0,3}(`{3,}|~{3,})/.exec(line)
  return m ? m[1].charAt(0) : ''
}

/** 逐行扫描 h1-h6(跳过围栏代码块), 分配去重 slug */
function scanHeadings(markdown: string): ScannedHeading[] {
  const out: ScannedHeading[] = []
  const slugger = createHeadingSlugger()
  const lines = String(markdown || '').split(/\r?\n/)
  let inFence = false
  let fenceMarker = ''
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const marker = isFenceStart(line)
    if (marker) {
      if (!inFence) { inFence = true; fenceMarker = marker }
      else if (marker === fenceMarker) { inFence = false; fenceMarker = '' }
      continue
    }
    if (inFence) continue
    const m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line)
    if (!m) continue
    const text = stripInlineMarkdown(m[2])
    if (!text) continue
    out.push({ level: m[1].length, text, slug: slugger(text), line: i })
  }
  return out
}

/** 从 Markdown 提取 h1-h6(跳过代码块), slug 与阅读渲染的标题 id 一致 */
export function extractHeadings(markdown: string): HeadingAnchor[] {
  return scanHeadings(markdown).map(({ level, text, slug }) => ({ level, text, slug }))
}

/** `[[页面#锚点]]` → { title, anchor }(按第一个 # 切分; 无锚点返回空串) */
export function splitWikiTarget(raw: string): WikiTarget {
  const text = String(raw || '').trim()
  const hash = text.indexOf('#')
  if (hash === -1) return { title: text, anchor: '' }
  return { title: text.slice(0, hash).trim(), anchor: text.slice(hash + 1).trim() }
}

/**
 * 解析 `[[` 补全的查询串:
 * - `页面`            → 页面阶段
 * - `页面#` / `页面#锚` → 标题阶段
 * - 结尾 `]]` 表示链接已闭合(光标在已有链接内), 用于「选中即关闭菜单」判定
 */
export function parseWikiMentionQuery(query: string): WikiMentionQuery {
  let q = String(query || '')
  const complete = q.endsWith(']]')
  if (complete) q = q.slice(0, -2)
  const hash = q.indexOf('#')
  if (hash === -1) return { raw: q, complete, pagePart: q.trim(), anchorPart: null }
  return {
    raw: q,
    complete,
    pagePart: q.slice(0, hash).trim(),
    anchorPart: q.slice(hash + 1).trim(),
  }
}

/** Markdown → 纯文本(去围栏代码块、标题/引用/列表标记与行内标记, 折叠空白) */
export function plainTextFromMarkdown(markdown: string): string {
  const lines: string[] = []
  let inFence = false
  let fenceMarker = ''
  for (const line of String(markdown || '').split(/\r?\n/)) {
    const marker = isFenceStart(line)
    if (marker) {
      if (!inFence) { inFence = true; fenceMarker = marker }
      else if (marker === fenceMarker) { inFence = false; fenceMarker = '' }
      continue
    }
    if (inFence) continue
    lines.push(line)
  }
  return stripInlineMarkdown(
    lines
      .join('\n')
      .replace(/^#{1,6}\s+/gm, '')
      .replace(/^\s{0,3}>\s?/gm, '')
      .replace(/^\s{0,3}([-*+]|\d+[.)])\s+/gm, ''),
  )
    .replace(/\s+/g, ' ')
    .trim()
}

/** 目标页摘要: 纯文本前 maxLen 字(悬浮预览用) */
export function markdownSnippet(markdown: string, maxLen = 200): string {
  const text = plainTextFromMarkdown(markdown)
  return text.length > maxLen ? `${text.slice(0, maxLen)}…` : text
}

/**
 * 锚点命中的标题段落: 从该标题起, 到下一个同级/更高级标题为止;
 * 未命中返回空串(调用方回退整页摘要)。正文同样截断到 maxLen。
 */
export function extractHeadingSection(markdown: string, anchor: string, maxLen = 200): string {
  const want = String(anchor || '').trim().toLowerCase()
  if (!want) return ''
  const lines = String(markdown || '').split(/\r?\n/)
  const headings = scanHeadings(markdown)
  const hit = headings.find(h => h.slug === want)
  if (!hit) return ''
  const body: string[] = []
  let inFence = false
  let fenceMarker = ''
  for (let i = hit.line + 1; i < lines.length; i++) {
    const line = lines[i]
    const marker = isFenceStart(line)
    if (marker) {
      if (!inFence) { inFence = true; fenceMarker = marker }
      else if (marker === fenceMarker) { inFence = false; fenceMarker = '' }
      body.push(line)
      continue
    }
    if (!inFence) {
      const m = /^(#{1,6})\s+/.exec(line)
      if (m && m[1].length <= hit.level) break
    }
    body.push(line)
  }
  const text = plainTextFromMarkdown(body.join('\n')) || hit.text
  return text.length > maxLen ? `${text.slice(0, maxLen)}…` : text
}

/**
 * `[[` 补全的自定义匹配器(替代 @tiptap/suggestion 默认匹配):
 * - 允许页面标题含空格(默认匹配器在空格处截断, 会导致「选中页面后输入 #」失效);
 * - 光标位于已有 `[[链接]]` 内时, 查询补上结尾 `]]`, 且 range 覆盖到 `]]`,
 *   便于命令整体替换(继续输入 # 进入标题列表);
 * - 光标在闭合链接之后(如 `[[A]]后有文字`)不触发, 避免误弹菜单。
 */
export function findWikiSuggestionMatch(config: any) {
  const { $position } = config
  const node = $position?.nodeBefore
  if (!node?.isText || !node.text) return null
  const text: string = node.text
  const start = text.lastIndexOf('[[')
  if (start < 0) return null
  if (start > 0 && !/[\s([{]/.test(text.charAt(start - 1))) return null
  const raw = text.slice(start + 2)
  if (/[[\]]/.test(raw)) return null
  const nodeAfter = $position.nodeAfter
  const closed = !!nodeAfter?.isText && String(nodeAfter.text).startsWith(']]')
  const from = $position.pos - text.length + start
  return {
    range: { from, to: closed ? $position.pos + 2 : $position.pos },
    query: raw + (closed ? ']]' : ''),
    text: text.slice(start),
  }
}

/**
 * markdown-it 插件: 给渲染出的 h1-h6 加稳定 id(slug + 序号去重),
 * 与 extractHeadings / 编辑器标题装饰使用同一 slug 规则。
 */
export function markdownHeadingAnchors(md: any): void {
  md.core.ruler.push('rag_heading_anchors', (state: any) => {
    const tokens: any[] = state.tokens || []
    const slugger = createHeadingSlugger()
    for (let i = 0; i < tokens.length; i++) {
      const token = tokens[i]
      if (token.type !== 'heading_open') continue
      const inline = tokens[i + 1]
      const text = stripInlineMarkdown(String(inline?.content || ''))
      token.attrSet('id', slugger(text))
    }
  })
}
