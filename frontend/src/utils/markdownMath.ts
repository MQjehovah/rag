/**
 * markdown-it 数学公式插件: 行内 `$...$` 与块级 `$$...$$`(KaTeX 语法)。
 *
 * 与编辑器节点 mathInline/mathBlock 配合:
 * - 解析: markdown-it 渲染为 `<span data-math-inline data-latex="...">` /
 *   `<div data-math-block data-latex="...">`, 由节点的 parseHTML 还原;
 * - 序列化: 节点自定义 serializer 输出 `$...$` / `$$\\n...\\n$$` 文本;
 *   公式含 `$`/换行/为空时退回 HTML 形式, 保证往返无损。
 *
 * 纯函数(无 DOM/无编辑器依赖), 供 tiptap-markdown 的 parse.setup 与单测共用。
 */

/** 输入规则: 行内公式(捕获组 1=完整 `$...$`, 2=latex)。前后需行首/空白, 内容不含首尾空白与 `$`。 */
export const MATH_INLINE_INPUT_RE = /(?:^|\s)(\$([^\s$](?:[^$\n]*[^\s$])?)\$)$/

/** 输入规则: 块级公式(捕获组 1=完整 `$$...$$`, 2=latex)。 */
export const MATH_BLOCK_INPUT_RE = /(?:^|\s)(\$\$([^$]+)\$\$)$/

const DOLLAR = 0x24

const escapeHtmlAttr = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

export function renderMathInlineHtml(latex: string): string {
  return `<span data-math-inline data-latex="${escapeHtmlAttr(latex)}"></span>`
}

export function renderMathBlockHtml(latex: string): string {
  return `<div data-math-block data-latex="${escapeHtmlAttr(latex)}"></div>`
}

/**
 * 行内规则: `$...$`。
 * - 不跨行、内容非空且不以空白开头/结尾(避免把 "价格 $5 到 $10" 误判);
 * - `$$` 由块级规则处理, 这里直接让行;
 * - 已注册在 escape 规则之后, `\$` 由转义规则先行消费。
 */
function mathInlineRule(state: any, silent: boolean): boolean {
  const start = state.pos
  if (state.src.charCodeAt(start) !== DOLLAR) return false
  const max = state.posMax
  if (state.pos + 1 >= max) return false
  if (state.src.charCodeAt(state.pos + 1) === DOLLAR) return false
  if (start > 0 && state.src.charCodeAt(start - 1) === DOLLAR) return false
  let end = start + 1
  while (end < max) {
    const code = state.src.charCodeAt(end)
    if (code === DOLLAR) break
    if (code === 0x0a /* \n */) return false
    end++
  }
  if (end >= max) return false
  const content = state.src.slice(start + 1, end)
  if (!content || /^\s|\s$/.test(content)) return false
  if (silent) return true
  state.pos = end + 1
  const token = state.push('math_inline', 'math', 0)
  token.markup = '$'
  token.content = content
  return true
}

/**
 * 块级规则: 行首(缩进 <4)的 `$$`, 支持单行 `$$x$$` 与多行 `$$\n...\n$$`。
 */
function mathBlockRule(state: any, startLine: number, endLine: number, silent: boolean): boolean {
  const start = state.bMarks[startLine] + state.tShift[startLine]
  const max = state.eMarks[startLine]
  if (start + 2 > max) return false
  if (state.src.charCodeAt(start) !== DOLLAR || state.src.charCodeAt(start + 1) !== DOLLAR) return false
  if (state.sCount[startLine] - state.blkIndent >= 4) return false

  let content = ''
  let nextLine = startLine
  let closed = false
  const firstLine = state.src.slice(start + 2, max)
  const singleClose = firstLine.indexOf('$$')
  if (singleClose >= 0) {
    content = firstLine.slice(0, singleClose)
    nextLine = startLine + 1
    closed = true
  } else {
    content = firstLine
    for (nextLine = startLine + 1; nextLine < endLine; nextLine++) {
      const lineStart = state.bMarks[nextLine] + state.tShift[nextLine]
      const lineEnd = state.eMarks[nextLine]
      const line = state.src.slice(lineStart, lineEnd)
      const closeIdx = line.indexOf('$$')
      if (closeIdx >= 0) {
        content += '\n' + line.slice(0, closeIdx)
        nextLine += 1
        closed = true
        break
      }
      content += '\n' + line
    }
  }
  if (!closed) return false
  if (silent) return true

  const token = state.push('math_block', 'math', 0)
  token.block = true
  token.content = content.replace(/^\n+|\n+$/g, '')
  token.map = [startLine, nextLine]
  state.line = nextLine
  return true
}

/** 把数学规则安装到 markdown-it 实例(每个实例只装一次)。 */
export function installMarkdownMath(md: any): void {
  if (!md || md.__ragMathInstalled) return
  md.__ragMathInstalled = true
  md.inline.ruler.after('escape', 'math_inline', mathInlineRule)
  md.block.ruler.before('paragraph', 'math_block', mathBlockRule)
  md.renderer.rules.math_inline = (tokens: any[], idx: number) => renderMathInlineHtml(String(tokens[idx].content))
  md.renderer.rules.math_block = (tokens: any[], idx: number) => renderMathBlockHtml(String(tokens[idx].content))
}

/** 节点 → Markdown(行内): 常规 `$...$`; 空/含 `$`/含换行时用 HTML 兜底保往返。 */
export function serializeMathInlineMarkdown(state: any, node: any): void {
  const latex = String(node.attrs?.latex ?? '')
  if (!latex || latex.includes('$') || /[\r\n]/.test(latex)) {
    state.write(renderMathInlineHtml(latex))
  } else {
    state.write(`$${latex}$`)
  }
}

/** 节点 → Markdown(块级): 常规 `$$\n...\n$$`; 空/含 `$` 时用 HTML 兜底保往返。 */
export function serializeMathBlockMarkdown(state: any, node: any): void {
  const latex = String(node.attrs?.latex ?? '')
  if (!latex || latex.includes('$')) {
    state.write(renderMathBlockHtml(latex))
  } else {
    state.write(`$$\n${latex}\n$$`)
  }
  state.closeBlock(node)
}
