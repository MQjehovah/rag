/**
 * markdown-it 脚注插件: 行内引用 `[^1]` 与定义行 `[^1]: 文本`。
 *
 * 与编辑器节点 footnoteRef/footnoteItem/footnotes 配合:
 * - 解析: markdown-it 渲染为 `<sup data-fn="1">[^1]</sup>` 与
 *   `<div data-footnotes><div data-footnote="1">…</div></div>`, 由节点的 parseHTML 还原;
 * - 序列化: FootnoteRef 输出 `[^1]`; Footnotes 逐条输出 `[^1]: 内容`(内容按行内
 *   Markdown 序列化), 重新解析时由本插件的块级规则还原为同一个脚注区块。
 *
 * `[^1]: 文本` 会被 markdown-it 默认的 reference(链接引用定义)规则吞掉,
 * 故块级规则注册在 reference 之前; 行内规则注册在 link 之前。
 * 纯函数(无 DOM/无编辑器依赖), 供 tiptap-markdown 的 parse.setup 与单测共用。
 */

/** 输入规则: 行内脚注引用(捕获组 1=完整 `[^n]`, 2=label)。 */
export const FOOTNOTE_REF_INPUT_RE = /(\[\^([^\s\]]+)\])$/

/** 定义行匹配: `[^label]: 内容`。 */
const FOOTNOTE_DEF_RE = /^\[\^([^\s\]]+)\]:[ \t]?(.*)$/

const escapeHtmlAttr = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

const escapeHtmlText = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

export function renderFootnoteRefHtml(label: string): string {
  return `<sup data-fn="${escapeHtmlAttr(label)}">[^${escapeHtmlText(label)}]</sup>`
}

/**
 * 行内规则: `[^label]`(label 不含空白/`]`/`[`)。注册在 link 规则之前,
 * 避免被链接/引用语法抢先解析。
 */
function footnoteRefRule(state: any, silent: boolean): boolean {
  const start = state.pos
  if (state.src.charCodeAt(start) !== 0x5b /* [ */) return false
  if (state.src.charCodeAt(start + 1) !== 0x5e /* ^ */) return false
  const max = state.posMax
  let pos = start + 2
  while (pos < max) {
    const code = state.src.charCodeAt(pos)
    if (code === 0x5d /* ] */) break
    if (code === 0x0a || code === 0x20 || code === 0x09 || code === 0x5b /* [ */) return false
    pos++
  }
  if (pos >= max) return false
  const label = state.src.slice(start + 2, pos)
  if (!label) return false
  if (silent) return true
  state.pos = pos + 1
  const token = state.push('footnote_ref', 'sup', 0)
  token.meta = { label }
  return true
}

/**
 * 块级规则: 连续的定义行合成一个脚注区块; 每条内容走行内解析(加粗/链接等可用),
 * 支持 2 空格以上缩进的续行(拼接为同一段)。
 */
function footnoteDefRule(state: any, startLine: number, endLine: number, silent: boolean): boolean {
  const lineStart = state.bMarks[startLine] + state.tShift[startLine]
  const lineEnd = state.eMarks[startLine]
  const first = FOOTNOTE_DEF_RE.exec(state.src.slice(lineStart, lineEnd))
  if (!first) return false
  if (silent) return true

  const items: { label: string; content: string }[] = []
  let nextLine = startLine
  while (nextLine < endLine) {
    const ls = state.bMarks[nextLine] + state.tShift[nextLine]
    const le = state.eMarks[nextLine]
    const m = FOOTNOTE_DEF_RE.exec(state.src.slice(ls, le))
    if (!m) break
    const contentLines = [m[2]]
    let j = nextLine + 1
    while (j < endLine) {
      const raw = state.src.slice(state.bMarks[j], state.eMarks[j])
      if (!raw.trim()) break
      if (!/^\s{2,}/.test(raw)) break
      if (FOOTNOTE_DEF_RE.test(raw.trim())) break
      contentLines.push(raw.replace(/^\s{2,4}/, ''))
      j++
    }
    items.push({ label: m[1], content: contentLines.join(' ').trim() })
    nextLine = j
  }

  const open = state.push('footnote_block_open', 'div', 1)
  open.map = [startLine, nextLine]
  for (const item of items) {
    const itemOpen = state.push('footnote_item_open', 'div', 1)
    itemOpen.meta = { label: item.label }
    const inline = state.push('inline', '', 0)
    inline.content = item.content
    inline.map = [startLine, nextLine]
    inline.children = []
    state.push('footnote_item_close', 'div', -1)
  }
  state.push('footnote_block_close', 'div', -1)
  state.line = nextLine
  return true
}

/** 把脚注规则安装到 markdown-it 实例(每个实例只装一次)。 */
export function installMarkdownFootnotes(md: any): void {
  if (!md || md.__ragFootnotesInstalled) return
  md.__ragFootnotesInstalled = true
  md.inline.ruler.before('link', 'footnote_ref', footnoteRefRule)
  md.block.ruler.before('reference', 'footnote_def', footnoteDefRule)
  md.renderer.rules.footnote_ref = (tokens: any[], idx: number) =>
    renderFootnoteRefHtml(String(tokens[idx].meta?.label ?? ''))
  md.renderer.rules.footnote_block_open = () => '<div data-footnotes>\n'
  md.renderer.rules.footnote_block_close = () => '</div>\n'
  md.renderer.rules.footnote_item_open = (tokens: any[], idx: number) =>
    `<div data-footnote="${escapeHtmlAttr(String(tokens[idx].meta?.label ?? ''))}">\n`
  md.renderer.rules.footnote_item_close = () => '</div>\n'
}

/** 节点 → Markdown(引用): `[^label]`。 */
export function serializeFootnoteRefMarkdown(state: any, node: any): void {
  const label = String(node.attrs?.label ?? '')
  state.write(`[^${label}]`)
}

/** 节点 → Markdown(脚注区块): 逐条 `[^label]: 内容`。 */
export function serializeFootnotesMarkdown(state: any, node: any): void {
  const items: any[] = []
  node.forEach((item: any) => items.push(item))
  if (!items.length) return
  for (const item of items) {
    const label = String(item.attrs?.label ?? '')
    state.write(`[^${label}]: `)
    state.renderInline(item)
    state.ensureNewLine()
  }
  state.closeBlock(node)
}

/** 计算下一个自动脚注编号: 取文档中引用/条目最大数字 + 1(非数字 label 忽略)。 */
export function nextFootnoteLabel(doc: any): string {
  let max = 0
  doc?.descendants?.((node: any) => {
    const name = node?.type?.name
    if (name !== 'footnoteRef' && name !== 'footnoteItem') return
    const n = parseInt(String(node.attrs?.label ?? ''), 10)
    if (!Number.isNaN(n) && n > max) max = n
  })
  return String(max + 1)
}
