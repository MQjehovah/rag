/**
 * 超大表格折叠扩展: 行数 > TABLE_FOLD_ROWS 的表格默认只显示前
 * TABLE_FOLD_PREVIEW 行正文 + 「展开全部」按钮, 点击可展开/收起。
 *
 * 通过 ProseMirror 装饰实现, 不改动文档内容与 Markdown 序列化:
 * - table 节点加 `table-folded`(/`table-folded-head`)类, CSS 隐藏多余行;
 * - 表格后挂一个 widget 按钮切换展开状态(CSS 与按钮均为展示层)。
 *
 * 光标进入隐藏行自动展开(R4a): 装饰随 selection 更新, 选区进入被隐藏的行时
 * 自动把该折叠标记为已展开; 一旦自动展开, 光标移出后保持展开到本次会话结束
 * (手动「收起」仍可折叠, 由 plugin state 的"选区是否在隐藏行内"迁移检测保证
 * 手动收起不会被下一次选区更新立刻顶回; 再次进入隐藏行才会重新自动展开)。
 */
import { Extension } from '@tiptap/core'
import type { Node as PMNode } from '@tiptap/pm/model'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

export const TABLE_FOLD_ROWS = 50
export const TABLE_FOLD_PREVIEW = 20

export const tableFoldKey = new PluginKey('tableFold')

/** 折叠时应隐藏的第一行 index(0-based); 不满足折叠条件时返回 null。 */
export function hiddenRowStart(table: PMNode): number | null {
  const firstRow = table.firstChild
  const hasHead = !!firstRow?.firstChild && firstRow.firstChild.type.name === 'tableHeader'
  const bodyRows = Math.max(0, table.childCount - (hasHead ? 1 : 0))
  if (bodyRows <= TABLE_FOLD_ROWS) return null
  return hasHead ? 1 + TABLE_FOLD_PREVIEW : TABLE_FOLD_PREVIEW
}

export const TableFold = Extension.create({
  name: 'tableFold',

  addProseMirrorPlugins() {
    // 展开状态按表格起始位置记录(文档编辑后位置会漂移, 仅作会话内的展示状态)
    const expanded = new Set<number>()

    return [
      new Plugin({
        key: tableFoldKey,
        state: {
          init: () => new Set<number>(),
          /** 记录"选区当前位于隐藏行内"的表格, 用于检测进入隐藏行的迁移。 */
          apply(tr, prev): Set<number> {
            const next = new Set<number>()
            const { from, to } = tr.selection
            tr.doc.descendants((node, pos) => {
              if (node.type.name !== 'table') return
              const start = hiddenRowStart(node)
              if (start === null) return
              let rowPos = pos + 1
              for (let i = 0; i < node.childCount; i++) {
                const row = node.child(i)
                if (i >= start && from < rowPos + row.nodeSize && to > rowPos) {
                  next.add(pos)
                  if (!prev.has(pos)) expanded.add(pos)
                  return
                }
                rowPos += row.nodeSize
              }
            })
            return next
          },
        },
        props: {
          decorations(state) {
            const decos: Decoration[] = []
            state.doc.descendants((node, pos) => {
              if (node.type.name !== 'table') return
              const start = hiddenRowStart(node)
              if (start === null) return
              const hasHead = start > TABLE_FOLD_PREVIEW
              const bodyRows = hasHead ? node.childCount - 1 : node.childCount
              const isExpanded = expanded.has(pos)
              if (!isExpanded) {
                decos.push(Decoration.node(pos, pos + node.nodeSize, {
                  class: hasHead ? 'table-folded table-folded-head' : 'table-folded',
                }))
              }
              decos.push(Decoration.widget(pos + node.nodeSize, (view) => {
                const btn = document.createElement('button')
                btn.type = 'button'
                btn.className = 'table-fold-toggle'
                btn.contentEditable = 'false'
                btn.textContent = isExpanded
                  ? `收起表格（共 ${bodyRows} 行）`
                  : `展开全部（共 ${bodyRows} 行，当前显示前 ${TABLE_FOLD_PREVIEW} 行）`
                btn.addEventListener('mousedown', (e) => {
                  e.preventDefault()
                  e.stopPropagation()
                  if (expanded.has(pos)) expanded.delete(pos)
                  else expanded.add(pos)
                  view.dispatch(view.state.tr.setMeta(tableFoldKey, true))
                })
                return btn
              }, { side: 1 }))
            })
            return DecorationSet.create(state.doc, decos)
          },
        },
      }),
    ]
  },
})
