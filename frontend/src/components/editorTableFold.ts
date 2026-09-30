/**
 * 超大表格折叠扩展: 行数 > TABLE_FOLD_ROWS 的表格默认只显示前
 * TABLE_FOLD_PREVIEW 行正文 + 「展开全部」按钮, 点击可展开/收起。
 *
 * 通过 ProseMirror 装饰实现, 不改动文档内容与 Markdown 序列化:
 * - table 节点加 `table-folded`(/`table-folded-head`)类, CSS 隐藏多余行;
 * - 表格后挂一个 widget 按钮切换展开状态(CSS 与按钮均为展示层)。
 */
import { Extension } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

export const TABLE_FOLD_ROWS = 50
export const TABLE_FOLD_PREVIEW = 20

export const tableFoldKey = new PluginKey('tableFold')

export const TableFold = Extension.create({
  name: 'tableFold',

  addProseMirrorPlugins() {
    // 展开状态按表格起始位置记录(文档编辑后位置会漂移, 仅作会话内的展示状态)
    const expanded = new Set<number>()

    return [
      new Plugin({
        key: tableFoldKey,
        props: {
          decorations(state) {
            const decos: Decoration[] = []
            state.doc.descendants((node, pos) => {
              if (node.type.name !== 'table') return
              const firstRow = node.firstChild
              const hasHead = !!firstRow?.firstChild && firstRow.firstChild.type.name === 'tableHeader'
              const bodyRows = Math.max(0, node.childCount - (hasHead ? 1 : 0))
              if (bodyRows <= TABLE_FOLD_ROWS) return
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
