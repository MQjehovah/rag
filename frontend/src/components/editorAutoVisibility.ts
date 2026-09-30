/**
 * 长文渲染优化(务实版, 不做全量虚拟滚动): 给 doc 的顶层块打 `.cv-auto` 装饰类,
 * 由 CSS `content-visibility: auto` 让视口外块跳过布局/绘制成本。
 *
 * 取舍说明: ProseMirror 全量虚拟化需要重构文档视图(段落测量/滚动锚定/协同光标
 * 定位全部受影响), 成本与回归风险高; 本版用浏览器原生 content-visibility 优化
 * 长文档滚动/绘制成本, 选区所在块/受保护块(如 AI ghost 建议)始终完整渲染。
 *
 * - 纯展示装饰: 不改文档内容、不参与 Markdown 序列化、不广播 Yjs;
 * - 选区变化用 meta-only 事务刷新装饰(不进 undo 栈);
 * - 含表格的块不打 cv-auto(R4a): content-visibility 会给祖先套上 contain,
 *   而 sticky 表头需要无 contain/无滚动容器的祖先链, 故整块跳过(表格一般不大,
 *   其折叠/折叠展开由 TableFold 负责);
 * - 打印兜底: 打印 CSS 强制 `content-visibility: visible`(见 Editor.vue), 与
 *   beforeprint 的图片/mermaid 展开路径一致。
 */
import { Extension } from '@tiptap/core'
import type { EditorState } from '@tiptap/pm/state'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import type { Node as PMNode } from '@tiptap/pm/model'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

export const cvAutoKey = new PluginKey('ragCvAuto')

export interface CvAutoOptions {
  /** 额外排除的顶层块(如 AI 幽灵建议所在块);返回 true 则不打 cv-auto。 */
  isBlockProtected?: (node: PMNode, pos: number, state: EditorState) => boolean
}

export const CvAutoVisibility = Extension.create<CvAutoOptions>({
  name: 'cvAutoVisibility',

  addProseMirrorPlugins() {
    const { isBlockProtected } = this.options
    return [
      new Plugin({
        key: cvAutoKey,
        props: {
          decorations(state) {
            const decos: Decoration[] = []
            const sel = state.selection
            state.doc.forEach((node, offset) => {
              const end = offset + node.nodeSize
              // 与选区相交的块完整渲染(含光标所在块)
              if (sel.from <= end && sel.to >= offset) return
              if (isBlockProtected?.(node, offset, state)) return
              // 表格(sticky 表头)要求祖先链无 contain, 整块跳过惰渲染
              if (blockHasTable(node)) return
              decos.push(Decoration.node(offset, end, { class: 'cv-auto' }))
            })
            return decos.length ? DecorationSet.create(state.doc, decos) : null
          },
        },
        // 选区变化的 refresh 事务: 只有 setMeta, 无 doc 改动 → 不产生历史步骤
        appendTransaction(trs, _oldState, newState) {
          if (!trs.some(tr => tr.selectionSet)) return null
          return newState.tr.setMeta(cvAutoKey, true)
        },
      }),
    ]
  },
})

/** 块自身或后代含 table(表格或其嵌套容器, 如 callout/列表); 文本块快速短路。 */
export function blockHasTable(node: PMNode): boolean {
  if (node.type.name === 'table') return true
  if (node.isTextblock || node.isLeaf) return false
  let found = false
  node.descendants((child) => {
    if (child.type.name === 'table') {
      found = true
      return false
    }
    return !found
  })
  return found
}
