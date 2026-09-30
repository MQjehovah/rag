/**
 * 块级操作工具(纯函数, 便于自测): 顶层块选取 + 缩进变更。
 *
 * 缩进实现: 段落/标题/引用被包裹进 `indentBlock` 容器节点(editorExt),
 * 容器带 indent(1..4) 属性, Markdown 往返走 HTML 兜底 `<div data-indent="N">`;
 * 列表的缩进不在此处处理(由 TipTap 原生 sinkListItem/liftListItem 负责)。
 */
import { NodeRange } from 'prosemirror-model'
import type { Node as PMNode, Schema } from 'prosemirror-model'
import type { Transaction } from 'prosemirror-state'

export interface TopBlock {
  pos: number
  end: number
  index: number
  node: PMNode
}

export const INDENT_MAX = 4
const INDENTABLE = ['paragraph', 'heading', 'blockquote']

/**
 * 取与 [from, to] 相交的顶层块:
 * - 折叠选区返回光标所在块(边界归后一个块, 文末归最后一个块), 与既有 topRange 语义一致;
 * - 区间选区返回所有相交块(尾端恰好落在块起点时不含后块)。
 */
export function topBlocksInRange(doc: PMNode, from: number, to: number): TopBlock[] {
  const out: TopBlock[] = []
  const size = doc.content.size
  const a = Math.max(0, Math.min(from, size))
  const b = Math.max(0, Math.min(to, size))
  if (a === b) {
    let chosen: TopBlock | null = null
    doc.forEach((node, offset, index) => {
      if (chosen) return
      if (offset <= a && a < offset + node.nodeSize) {
        chosen = { pos: offset, end: offset + node.nodeSize, index, node }
      }
    })
    if (!chosen && doc.childCount > 0) {
      const index = doc.childCount - 1
      const node = doc.child(index)
      const pos = size - node.nodeSize
      chosen = { pos, end: pos + node.nodeSize, index, node }
    }
    return chosen ? [chosen] : []
  }
  const lo = Math.min(a, b)
  const hi = Math.max(a, b)
  doc.forEach((node, offset, index) => {
    const end = offset + node.nodeSize
    if (offset < hi && end > lo) out.push({ pos: offset, end, index, node })
  })
  return out
}

/** 列表块(其缩进交给原生 sink/lift) */
export function isListBlock(node: PMNode): boolean {
  const name = node?.type?.name
  return name === 'bulletList' || name === 'orderedList' || name === 'taskList'
}

/**
 * 对单个顶层块应用缩进变更(直接写进事务):
 * - indentBlock: 级别 +delta(0 则解包还原); 触边界返回 false
 * - 段落/标题/引用: delta>0 时包一层 indentBlock(delta<=0 无操作)
 * 返回是否产生变更。
 */
export function applyBlockIndent(tr: Transaction, schema: Schema, block: TopBlock, delta: number): boolean {
  const type = schema.nodes.indentBlock
  if (!type) return false
  if (block.node.type === type) {
    const current = Math.max(1, Math.min(INDENT_MAX, Number(block.node.attrs.indent) || 1))
    const next = Math.max(0, Math.min(INDENT_MAX, current + delta))
    if (next === current) return false
    if (next === 0) tr.replaceWith(block.pos, block.end, block.node.content)
    else tr.setNodeMarkup(block.pos, undefined, { ...block.node.attrs, indent: next })
    return true
  }
  if (delta <= 0) return false
  if (!(INDENTABLE as readonly string[]).includes(block.node.type.name)) return false
  tr.wrap(
    new NodeRange(tr.doc.resolve(block.pos), tr.doc.resolve(block.end), 0),
    [{ type, attrs: { indent: 1 } }],
  )
  return true
}
