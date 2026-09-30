/**
 * 粘贴 HTML 净化(网页 / Word / 其它站点的富文本)。
 *
 * 仅保留: 标题(p/h1-h6)/段落/列表(ul/ol/li)/表格/链接/加粗/斜体/下划线/删除线/代码/引用/图片,
 * 以及编辑器既有节点所需的 marker(callout/折叠块/附件/引用卡片/任务列表/高亮)。
 * 剥离 style/class/id/data-* (明确白名单的 marker 除外) 与 script/iframe 等;
 * 单元格(td/th)的 style 仅保留安全的 background-color 并转为 `data-bg`;
 * 空段落(`<p></p>`/`<p><br></p>`/`<p>&nbsp;</p>`)折叠。
 *
 * 不依赖 DOM(纯函数), 供 transformPastedHTML 与单测共用。
 */

import { extractSafeBackgroundFromStyle } from './tableCellColor'

interface Frame {
  /** 原始标签名(小写); 未保留的标签用于配对闭合 */
  name: string
  /** 由 style 派生的格式化打开标签(如 `<strong>`), 关闭时需按逆序闭合 */
  wrappers: string[]
  /** 是否输出过真实开标签 */
  kept: boolean
}

/** 整段丢弃内容的标签(含嵌套) */
const DROP_CONTENT = new Set([
  'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template',
  'noscript', 'canvas', 'video', 'audio', 'form', 'select', 'textarea', 'button',
  'head',
])

/** 直接丢弃标签本身(内容照常处理) */
const DROP_TAG = new Set(['html', 'body', 'meta', 'link', 'base', 'input', 'o:p', 'w:sdt', 'xml', 'title'])

/** 保留的标签(其余标签一律"拆壳"保留内容) */
const KEEP_TAGS = new Set([
  'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br', 'hr',
  'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
  'strong', 'b', 'em', 'i', 'u', 's', 'del', 'strike', 'mark',
  'a', 'img', 'span', 'sup',
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'caption', 'colgroup', 'col',
  'div', 'details', 'summary',
])

/** 自闭合/空标签 */
const VOID_TAGS = new Set(['br', 'hr', 'img', 'col'])

/** 语义已覆盖对应样式、无需再包一层格式标签 */
const SEMANTIC_FOR: Record<string, string[]> = {
  strong: ['strong'],
  b: ['strong'],
  em: ['em'],
  i: ['em'],
  u: ['u'],
  s: ['s'],
  del: ['s'],
  strike: ['s'],
  h1: ['strong'],
  h2: ['strong'],
  h3: ['strong'],
  h4: ['strong'],
  h5: ['strong'],
  h6: ['strong'],
}

/** 按标签白名单保留的属性 */
const KEEP_ATTRS: Record<string, string[]> = {
  p: [], h1: [], h2: [], h3: [], h4: [], h5: [], h6: [], br: [], hr: [],
  ul: ['data-type'], ol: [], li: ['data-type', 'data-checked'],
  blockquote: [], pre: [], code: [],
  strong: [], b: [], em: [], i: [], u: [], s: [], del: [], strike: [], mark: [],
  a: ['href', 'title'],
  img: ['src', 'alt', 'title', 'width'],
  span: ['data-math-inline', 'data-latex'],
  sup: ['data-fn', 'data-fn-id'],
  table: [], thead: [], tbody: [], tfoot: [], tr: [], td: ['colspan', 'rowspan', 'data-bg'],
  th: ['colspan', 'rowspan', 'data-bg'], caption: [], colgroup: [], col: ['span'],
  div: ['data-callout', 'data-attachment', 'data-url', 'data-name', 'data-size', 'data-mime', 'data-math-block', 'data-latex', 'data-footnotes', 'data-footnote', 'data-indent', 'data-citation', 'data-id', 'data-kind', 'data-title', 'data-summary'],
  details: ['data-toggle', 'open'], summary: [],
}

/** 允许保留的 class(既有节点的 marker) */
const MARKER_CLASSES: Record<string, (cls: string) => boolean> = {
  div: cls => cls === 'toggle-content',
  code: cls => /^language-[\w-]+$/.test(cls),
}

const safeHref = (value: string): string | null => {
  const v = value.trim()
  if (!v) return null
  const lower = v.toLowerCase()
  if (lower.startsWith('javascript:') || lower.startsWith('vbscript:') || lower.startsWith('data:text/html')) return null
  return v
}

const safeSrc = (value: string): string | null => {
  const v = value.trim()
  if (!v) return null
  const lower = v.toLowerCase()
  if (lower.startsWith('javascript:') || lower.startsWith('vbscript:')) return null
  return v
}

/** 从 style 中解析需要保留的格式化效果(Word/网页大量用 style 表达加粗/斜体等)。 */
function stylesToMarks(style: string): string[] {
  const marks: string[] = []
  const weight = (/font-weight\s*:\s*([^;]+)/i.exec(style)?.[1] || '').trim().toLowerCase()
  if (weight === 'bold' || weight === 'bolder' || Number(weight) >= 600) marks.push('strong')
  const fontStyle = (/font-style\s*:\s*([^;]+)/i.exec(style)?.[1] || '').trim().toLowerCase()
  if (fontStyle === 'italic' || fontStyle === 'oblique') marks.push('em')
  const decoration = (/text-decoration[^:]*:\s*([^;]+)/i.exec(style)?.[1] || '').toLowerCase()
  if (decoration.includes('line-through')) marks.push('s')
  if (decoration.includes('underline')) marks.push('u')
  return marks
}

const wrapperTag: Record<string, string> = { strong: 'strong', em: 'em', s: 's', u: 'u' }

interface ParsedTag {
  name: string
  attrs: string
  selfClosing: boolean
  closing: boolean
}

/** 扫描一个标签(从 '<' 到匹配的 '>', 处理引号内的 '>'); 返回 null 表示不是标签。 */
function parseTag(html: string, start: number): { tag: ParsedTag; end: number } | null {
  let i = start + 1
  let quote = ''
  while (i < html.length) {
    const ch = html[i]
    if (quote) {
      if (ch === quote) quote = ''
    } else if (ch === '"' || ch === "'") {
      quote = ch
    } else if (ch === '>') {
      break
    }
    i++
  }
  if (i >= html.length) return null
  const raw = html.slice(start + 1, i).trim()
  if (!raw || raw.startsWith('!') || raw.startsWith('?')) return null

  const closing = raw.startsWith('/')
  const body = closing ? raw.slice(1) : raw
  const selfClosing = !closing && /\/\s*$/.test(body)
  const cleanBody = selfClosing ? body.replace(/\/\s*$/, '') : body
  const nameMatch = /^([a-zA-Z][\w:-]*)/.exec(cleanBody)
  if (!nameMatch) return null
  return {
    tag: {
      name: nameMatch[1].toLowerCase(),
      attrs: cleanBody.slice(nameMatch[1].length),
      selfClosing,
      closing,
    },
    end: i + 1,
  }
}

/** 解析属性串为 [name, value][]; 保留无值属性(值为 '')。 */
function parseAttrs(attrText: string): [string, string][] {
  const out: [string, string][] = []
  const re = /([\w:-]+)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g
  let m: RegExpExecArray | null
  while ((m = re.exec(attrText)) !== null) {
    out.push([m[1].toLowerCase(), m[2] ?? m[3] ?? m[4] ?? ''])
  }
  return out
}

function decodeEntities(value: string): string {
  return value
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&nbsp;/gi, '\u00a0')
    .replace(/&#x([0-9a-f]+);/gi, (_m, hex: string) => safeCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_m, dec: string) => safeCodePoint(Number(dec)))
    .replace(/&amp;/gi, '&')
}

function safeCodePoint(code: number): string {
  try {
    return code >= 0 && code <= 0x10ffff ? String.fromCodePoint(code) : ''
  } catch {
    return ''
  }
}

/** 属性值先解实体再统一转义, 避免对已转义内容二次转义(&amp; → &amp;amp;)。 */
function escapeAttr(value: string): string {
  return decodeEntities(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function renderOpenTag(name: string, attrs: [string, string][]): string {
  const allowed = KEEP_ATTRS[name] ?? []
  const parts: string[] = []
  for (const [key, value] of attrs) {
    if (key === 'style') {
      // 单元格底色: style 整段剥离, 仅把安全的 background-color 转成 data-bg(parseHTML 还原)
      if (name === 'td' || name === 'th') {
        const bg = extractSafeBackgroundFromStyle(value)
        if (bg) parts.push(`data-bg="${escapeAttr(bg)}"`)
      }
      continue
    }
    if (key === 'class') {
      const marker = MARKER_CLASSES[name]
      if (marker && marker(value.trim())) parts.push(`class="${escapeAttr(value.trim())}"`)
      continue
    }
    if (!allowed.includes(key)) continue
    if (key === 'href') {
      const href = safeHref(value)
      if (href) parts.push(`href="${escapeAttr(href)}"`)
      continue
    }
    if (key === 'src') {
      const src = safeSrc(value)
      if (src) parts.push(`src="${escapeAttr(src)}"`)
      continue
    }
    // 无值属性(open/data-toggle/data-attachment 等)输出裸名, 与 HTML 习惯一致
    parts.push(value === '' ? key : `${key}="${escapeAttr(value)}"`)
  }
  const attrText = parts.length ? ' ' + parts.join(' ') : ''
  return `<${name}${attrText}>`
}

const EMPTY_PARA = /<p>(?:<br\s*\/?>|&nbsp;|\u00a0|\s)*<\/p>/gi

/** 迭代折叠空段落(替换可能使相邻空段变成新的邻接, 多跑几轮直到稳定)。 */
function collapseEmptyParagraphs(html: string): string {
  let prev = html
  for (let round = 0; round < 4; round++) {
    const next = prev.replace(EMPTY_PARA, '')
    if (next === prev) break
    prev = next
  }
  return prev
}

export function sanitizePastedHTML(html: string): string {
  if (!html || typeof html !== 'string') return ''

  let out = ''
  const stack: Frame[] = []
  let skipTag = ''
  let skipDepth = 0
  let i = 0

  while (i < html.length) {
    const lt = html.indexOf('<', i)
    if (lt === -1) {
      if (!skipTag) out += html.slice(i)
      break
    }
    if (lt > i && !skipTag) out += html.slice(i, lt)

    // 注释/条件注释、DOCTYPE 等 `<![...]>` / `<?...>` 整段丢弃
    if (html.startsWith('<!--', lt)) {
      const end = html.indexOf('-->', lt + 4)
      i = end === -1 ? html.length : end + 3
      continue
    }
    if (html.startsWith('<!', lt) || html.startsWith('<?', lt)) {
      const end = html.indexOf('>', lt + 2)
      i = end === -1 ? html.length : end + 1
      continue
    }

    const parsed = parseTag(html, lt)
    if (!parsed) {
      if (!skipTag) out += '&lt;'
      i = lt + 1
      continue
    }
    i = parsed.end
    const { name, closing, selfClosing } = parsed.tag

    if (skipTag) {
      if (name === skipTag) {
        if (closing) {
          skipDepth--
          if (skipDepth <= 0) { skipTag = ''; skipDepth = 0 }
        } else if (!selfClosing) {
          skipDepth++
        }
      }
      continue
    }

    if (DROP_CONTENT.has(name)) {
      if (!closing && !selfClosing) { skipTag = name; skipDepth = 1 }
      continue
    }
    if (DROP_TAG.has(name)) continue

    if (closing) {
      // 找到最近的同名帧; 其间未闭合的帧一并收尾, 保证输出标签平衡
      let idx = -1
      for (let k = stack.length - 1; k >= 0; k--) {
        if (stack[k].name === name) { idx = k; break }
      }
      if (idx === -1) continue
      while (stack.length > idx) {
        const frame = stack.pop()!
        for (let k = frame.wrappers.length - 1; k >= 0; k--) out += `</${frame.wrappers[k]}>`
        if (frame.kept) out += `</${frame.name}>`
      }
      continue
    }

    if (selfClosing || VOID_TAGS.has(name)) {
      if (KEEP_TAGS.has(name)) out += renderOpenTag(name, parseAttrs(parsed.tag.attrs))
      continue
    }

    const attrs = parseAttrs(parsed.tag.attrs)
    const kept = KEEP_TAGS.has(name)
    const frame: Frame = { name, wrappers: [], kept }

    if (kept) out += renderOpenTag(name, attrs)

    // span/div/section 等容器(或不含语义的标签)若带 style 表达加粗/斜体等, 用语义标签包一层
    if (name !== 'pre' && name !== 'code') {
      const style = attrs.find(([k]) => k === 'style')?.[1] || ''
      const own = SEMANTIC_FOR[name] || []
      for (const mark of stylesToMarks(style)) {
        if (own.includes(mark)) continue
        out += `<${wrapperTag[mark]}>`
        frame.wrappers.push(wrapperTag[mark])
      }
    }

    stack.push(frame)
  }

  // 收尾: 闭合所有未闭合帧
  while (stack.length) {
    const frame = stack.pop()!
    for (let k = frame.wrappers.length - 1; k >= 0; k--) out += `</${frame.wrappers[k]}>`
    if (frame.kept) out += `</${frame.name}>`
  }

  return collapseEmptyParagraphs(out).trim()
}
