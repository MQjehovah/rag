/**
 * 表格行列移动(R4a, 菜单式)。
 *
 * 为什么不做拖拽: 表格内拖拽与文本选区、浏览器原生拖拽(选中文本/图片)相互
 * 冲突, 且拖拽手柄会抢占单元格内的原生交互; v1 用右键菜单/浮动条按钮更稳,
 * 后续若要拖拽可在此基础上加手柄(移动逻辑复用本模块)。
 *
 * 实现: 找到当前单元格 → 行/列 index → 用新行/列序列重建整个 table 节点并
 * 单事务 replaceWith(结构不变、尺寸不变, 表格后内容位置不受影响), 再把选区
 * 放回移动后的单元格。合并单元格场景按轴禁用: 行内存在 rowspan(跨行)禁行移动,
 * 存在 colspan(跨列)禁列移动, 避免破坏合并结构。
 */

import type { Node as PMNode } from '@tiptap/pm/model'
import { TextSelection, type EditorState, type Selection, type Transaction } from '@tiptap/pm/state'

export type MoveAxis = 'row' | 'column'
export type MoveDirection = 'up' | 'down' | 'left' | 'right'

export interface TableCellContext {
  table: PMNode
  /** 表格节点在文档中的起始位置 */
  tablePos: number
  /** 当前单元格节点 */
  cell: PMNode
  row: PMNode
  rowIndex: number
  /** 单元格在行内的 index */
  cellIndex: number
  /** 单元格节点起始位置 */
  cellPos: number
  /** 行内容起点相对表格内容起点的偏移 */
  rowOffset: number
  /** 单元格内容相对行内容起点的偏移 */
  cellOffset: number
}

/** 定位选区(或指定位置)所在的单元格及行/列 index。 */
export function findTableCellContext(state: EditorState, pos?: number): TableCellContext | null {
  const at = typeof pos === 'number' ? pos : state.selection.from
  const clamped = Math.max(0, Math.min(at, state.doc.content.size))
  const $pos = state.doc.resolve(clamped)
  for (let depth = $pos.depth; depth >= 3; depth--) {
    const cell = $pos.node(depth)
    if (cell.type.name !== 'tableCell' && cell.type.name !== 'tableHeader') continue
    const row = $pos.node(depth - 1)
    const table = $pos.node(depth - 2)
    if (row.type.name !== 'tableRow' || table.type.name !== 'table') return null

    const cellPos = $pos.before(depth)
    const rowPos = $pos.before(depth - 1)
    const tablePos = $pos.before(depth - 2)

    let rowIndex = -1
    let rowOffset = 0
    {
      let offset = 0
      for (let i = 0; i < table.childCount; i++) {
        if (rowPos === tablePos + 1 + offset) { rowIndex = i; rowOffset = offset; break }
        offset += table.child(i).nodeSize
      }
    }
    if (rowIndex === -1) return null

    let cellIndex = -1
    let cellOffset = 0
    {
      let offset = 0
      for (let i = 0; i < row.childCount; i++) {
        if (cellPos === rowPos + 1 + offset) { cellIndex = i; cellOffset = offset; break }
        offset += row.child(i).nodeSize
      }
    }
    if (cellIndex === -1) return null

    return { table, tablePos, cell, row, rowIndex, cellIndex, cellPos, rowOffset, cellOffset }
  }
  return null
}

function cellSpan(cell: PMNode, key: 'colspan' | 'rowspan'): number {
  return Math.max(1, Number(cell.attrs?.[key]) || 1)
}

/** 表格内是否存在跨行合并(rowspan > 1); 跨行时行移动会破坏合并结构。 */
export function tableHasRowSpan(table: PMNode): boolean {
  for (let i = 0; i < table.childCount; i++) {
    const row = table.child(i)
    for (let j = 0; j < row.childCount; j++) {
      if (cellSpan(row.child(j), 'rowspan') > 1) return true
    }
  }
  return false
}

/** 表格内是否存在跨列合并(colspan > 1); 跨列时列移动会破坏合并结构。 */
export function tableHasColSpan(table: PMNode): boolean {
  for (let i = 0; i < table.childCount; i++) {
    const row = table.child(i)
    for (let j = 0; j < row.childCount; j++) {
      if (cellSpan(row.child(j), 'colspan') > 1) return true
    }
  }
  return false
}

/** 各行单元格数一致(存在合并的表格通常不一致, 列移动一律禁用)。 */
export function tableRowsAreUniform(table: PMNode): boolean {
  if (table.childCount === 0) return false
  const count = table.firstChild!.childCount
  for (let i = 1; i < table.childCount; i++) {
    if (table.child(i).childCount !== count) return false
  }
  return true
}

/** 当前行能否上/下移(首末行、跨行合并时禁用)。 */
export function canMoveRow(state: EditorState, dir: MoveDirection): boolean {
  if (dir !== 'up' && dir !== 'down') return false
  const ctx = findTableCellContext(state)
  if (!ctx) return false
  if (dir === 'up' && ctx.rowIndex === 0) return false
  if (dir === 'down' && ctx.rowIndex >= ctx.table.childCount - 1) return false
  if (tableHasRowSpan(ctx.table)) return false
  return true
}

/** 当前列能否左/右移(首末列、跨列合并、行结构不一致时禁用)。 */
export function canMoveColumn(state: EditorState, dir: MoveDirection): boolean {
  if (dir !== 'left' && dir !== 'right') return false
  const ctx = findTableCellContext(state)
  if (!ctx) return false
  if (dir === 'left' && ctx.cellIndex === 0) return false
  if (dir === 'right' && ctx.cellIndex >= ctx.row.childCount - 1) return false
  if (tableHasColSpan(ctx.table)) return false
  if (!tableRowsAreUniform(ctx.table)) return false
  return true
}

function childrenOf(node: PMNode): PMNode[] {
  const out: PMNode[] = []
  node.forEach((child) => { out.push(child) })
  return out
}

/** 选区落入指定单元格内容起点(移动后把光标放回原单元格)。 */
function selectionInCell(tr: Transaction, cellPos: number): Selection | null {
  try {
    return TextSelection.near(tr.doc.resolve(cellPos + 1))
  } catch {
    return null
  }
}

/** 行上/下移: 与相邻行交换, 选区落在移动后的原单元格。 */
export function moveTableRow(state: EditorState, dir: MoveDirection): Transaction | null {
  if (!canMoveRow(state, dir)) return null
  const ctx = findTableCellContext(state)
  if (!ctx) return null
  const to = dir === 'up' ? ctx.rowIndex - 1 : ctx.rowIndex + 1
  const rows = childrenOf(ctx.table)
  const swapped = rows.slice()
  ;[swapped[ctx.rowIndex], swapped[to]] = [swapped[to], swapped[ctx.rowIndex]]
  const newTable = ctx.table.type.create(ctx.table.attrs, swapped)
  const newCellPos = ctx.tablePos + 2 + swapped.slice(0, to).reduce((n, r) => n + r.nodeSize, 0) + ctx.cellOffset
  const tr = state.tr.replaceWith(ctx.tablePos, ctx.tablePos + ctx.table.nodeSize, newTable)
  const selection = selectionInCell(tr, newCellPos)
  if (selection) tr.setSelection(selection)
  return tr
}

/** 列左/右移: 每行交换同 index 单元格, 选区落在移动后的原单元格。 */
export function moveTableColumn(state: EditorState, dir: MoveDirection): Transaction | null {
  if (!canMoveColumn(state, dir)) return null
  const ctx = findTableCellContext(state)
  if (!ctx) return null
  const to = dir === 'left' ? ctx.cellIndex - 1 : ctx.cellIndex + 1
  const rows = childrenOf(ctx.table)
  const newRows = rows.map((row) => {
    const cells = childrenOf(row)
    ;[cells[ctx.cellIndex], cells[to]] = [cells[to], cells[ctx.cellIndex]]
    return row.type.create(row.attrs, cells)
  })
  const newTable = ctx.table.type.create(ctx.table.attrs, newRows)
  const newCells = childrenOf(newRows[ctx.rowIndex])
  const cellOffset = newCells.slice(0, to).reduce((n, c) => n + c.nodeSize, 0)
  const rowOffset = newRows.slice(0, ctx.rowIndex).reduce((n, r) => n + r.nodeSize, 0)
  const newCellPos = ctx.tablePos + 2 + rowOffset + cellOffset
  const tr = state.tr.replaceWith(ctx.tablePos, ctx.tablePos + ctx.table.nodeSize, newTable)
  const selection = selectionInCell(tr, newCellPos)
  if (selection) tr.setSelection(selection)
  return tr
}
