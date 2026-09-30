/**
 * 表格节点 → Markdown 序列化(替代 tiptap-markdown 默认表格 serializer)。
 *
 * 默认实现的问题:
 * - 单元格内文本不转义 `|`, 含竖线的单元格会多切出列, 再次解析时内容错位/幂等失败;
 * - 单元格内的硬换行(hardBreak, 比如 `<br>`)按 prosemirror-markdown 输出 `\\\n`,
 *   换行直接撑破表格行, 二次解析变成两行/空行;
 * - 只渲染单元格第一个子块, 多段落单元格的后续内容静默丢失。
 *
 * 本实现:
 * - 常规表格输出 GFM 表格: 单元格文本转义 `|` → `\|`; hardBreak → `<br>`(行内 HTML);
 * - 单元格含 col/rowspan、多个块或非段落内容时, 回退整表 HTML(与 tiptap-markdown 的
 *   HTMLNode 兜底行为一致, 用 DOMSerializer 生成, 块级 HTML 前后加换行);
 * - 其余行为(表头分隔行 `---`、空单元格、表格后空行)与默认实现保持一致。
 *
 * 依赖 DOM 仅用于 HTML 兜底; 常规路径为纯字符串处理。
 */

import { DOMSerializer } from '@tiptap/pm/model'

/** 与 tiptap-markdown 一致: 表头必须在首行、正文行不得出现表头, 且无跨行/跨列。 */
function hasSpan(node: any): boolean {
  return (node.attrs?.colspan ?? 1) > 1 || (node.attrs?.rowspan ?? 1) > 1
}

/** 单元格底色(R4a): GFM 表格无法表达, 回退整表 HTML 以保证往返无损。 */
function hasCellBackground(node: any): boolean {
  return !!node.attrs?.backgroundColor
}

function childNodes(node: any): any[] {
  const out: any[] = []
  if (node) node.forEach((child: any) => { out.push(child) })
  return out
}

export function isMarkdownSerializableTable(node: any): boolean {
  const rows = childNodes(node)
  const firstRow = rows[0]
  if (!firstRow) return false
  if (childNodes(firstRow).some((cell) => cell.type.name !== 'tableHeader' || hasSpan(cell) || hasCellBackground(cell))) return false
  if (rows.slice(1).some((row) => childNodes(row).some((cell) => cell.type.name === 'tableHeader' || hasSpan(cell) || hasCellBackground(cell)))) return false
  return true
}

/** 单元格可无损表示为单行 Markdown: 空 / 单段落(含硬换行)。 */
function isSimpleCell(cell: any): boolean {
  if (cell.childCount === 0) return true
  if (cell.childCount > 1) return false
  return cell.firstChild.type.name === 'paragraph'
}

/** 单元格内容 → 单元格 Markdown(转义竖线, 硬换行转 `<br>`)。 */
function renderCellMarkdown(state: any, cell: any): string {
  if (cell.childCount === 0) return ''
  const start = state.out.length
  state.renderInline(cell.firstChild)
  let text = state.out.slice(start)
  state.out = state.out.slice(0, start)
  text = text.replace(/\|/g, '\\|')
  // hardBreak 输出 `\` + 换行; 文本内字面反斜杠已被 esc 双写, 故只剩换行前的最后一个 `\`
  text = text.replace(/\\\n/g, '<br>')
  return text
}

/** 节点 HTML(DOMSerializer; 顶层块级内容前后加换行, 对齐 tiptap-markdown 的 formatBlock)。 */
function serializeNodeHtml(node: any, parent: any): string {
  const schema = node.type.schema
  const dom = DOMSerializer.fromSchema(schema).serializeNode(node) as HTMLElement
  if (node.isBlock && parent?.type?.name === schema.topNodeType.name) {
    dom.innerHTML = dom.innerHTML.trim() ? `\n${dom.innerHTML}\n` : '\n'
  }
  return dom.outerHTML
}

/** tiptap-markdown 表格节点 serializer。 */
export function serializeTableMarkdown(state: any, node: any, parent: any): void {
  let fallback = !isMarkdownSerializableTable(node)
  const rows: any[] = []
  node.forEach((row: any) => { rows.push(row) })
  for (const row of rows) {
    row.forEach((cell: any) => { if (!isSimpleCell(cell) || hasCellBackground(cell)) fallback = true })
  }
  if (fallback) {
    state.write(serializeNodeHtml(node, parent))
    state.closeBlock(node)
    return
  }
  rows.forEach((row, i) => {
    state.write('| ')
    row.forEach((cell: any, j: number) => {
      if (j) state.write(' | ')
      state.write(renderCellMarkdown(state, cell))
    })
    state.write(' |')
    state.ensureNewLine()
    if (!i) {
      const delimiterRow = Array.from({ length: row.childCount }).map(() => '---').join(' | ')
      state.write(`| ${delimiterRow} |`)
      state.ensureNewLine()
    }
  })
  state.closeBlock(node)
}
