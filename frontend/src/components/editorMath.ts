import { InputRule, Node, mergeAttributes, nodeInputRule } from '@tiptap/core'

import {
  MATH_BLOCK_INPUT_RE,
  MATH_INLINE_INPUT_RE,
  installMarkdownMath,
  serializeMathBlockMarkdown,
  serializeMathInlineMarkdown,
} from '../utils/markdownMath'

const latexAttribute = {
  latex: {
    default: '',
    parseHTML: (el: HTMLElement) => el.getAttribute('data-latex') || '',
    renderHTML: (attrs: Record<string, any>) => ({ 'data-latex': String(attrs.latex ?? '') }),
  },
}

/**
 * 行内公式(原子节点)。输入 `$x$` 即时转换; 编辑器中由 MathNodeView 用
 * katex.renderToString 渲染, 双击编辑源代码; Markdown 序列化为 `$x$`。
 */
export const MathInline = Node.create({
  name: 'mathInline',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return { ...latexAttribute }
  },

  parseHTML() {
    return [{ tag: 'span[data-math-inline]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes(HTMLAttributes, { 'data-math-inline': '', class: 'math-inline' })]
  },

  addInputRules() {
    return [
      nodeInputRule({
        find: MATH_INLINE_INPUT_RE,
        type: this.type,
        getAttributes: (match: RegExpMatchArray) => ({ latex: match[2] }),
      }),
    ]
  },

  addStorage() {
    return {
      markdown: {
        serialize: serializeMathInlineMarkdown,
        parse: { setup: installMarkdownMath },
      },
    }
  },
})

/**
 * 块级公式(原子节点)。输入 `$$x$$` 即时转换; 整段匹配时直接替换段落,
 * 避免留下空段落; Markdown 序列化为 `$$\n...\n$$`。
 */
export const MathBlock = Node.create({
  name: 'mathBlock',
  group: 'block',
  atom: true,
  selectable: true,

  addAttributes() {
    return { ...latexAttribute }
  },

  parseHTML() {
    return [{ tag: 'div[data-math-block]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes(HTMLAttributes, { 'data-math-block': '', class: 'math-block' })]
  },

  addInputRules() {
    return [
      new InputRule({
        find: MATH_BLOCK_INPUT_RE,
        handler: ({ state, range, match }) => {
          const latex = match[2] || ''
          const block = state.schema.nodes.mathBlock.create({ latex })
          const $from = state.doc.resolve(range.from)
          const wholeParagraph = $from.parent.isTextblock
            && range.from === $from.start()
            && range.to === $from.end()
          // 与 nodeInputRule 一致: handleTextInput 触发时最后一个字符尚未写入文档
          state.tr.insertText(match[0][match[0].length - 1], range.to)
          if (wholeParagraph) {
            state.tr.replaceWith($from.before(1), $from.after(1), block)
          } else {
            const start = range.from + match[0].lastIndexOf(match[1])
            state.tr.replaceWith(start, start + match[1].length, block)
          }
        },
      }),
    ]
  },

  addStorage() {
    return {
      markdown: {
        serialize: serializeMathBlockMarkdown,
        parse: { setup: installMarkdownMath },
      },
    }
  },
})
