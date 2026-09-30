import { Node, mergeAttributes, nodeInputRule } from '@tiptap/core'
import { Plugin, PluginKey } from 'prosemirror-state'

import {
  FOOTNOTE_REF_INPUT_RE,
  installMarkdownFootnotes,
  serializeFootnoteRefMarkdown,
  serializeFootnotesMarkdown,
} from '../utils/markdownFootnotes'

const selectorEscape = (value: string): string => value.replace(/["\\]/g, '\\$&')

/** 高亮并滚动到目标元素(引用 ↔ 条目双向锚点)。 */
export function flashAndScroll(el: HTMLElement | null): void {
  if (!el) return
  try {
    el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  } catch { /* ignore */ }
  el.classList.add('fn-flash')
  window.setTimeout(() => el.classList.remove('fn-flash'), 1400)
}

export function findFootnoteItem(root: HTMLElement, label: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`[data-footnote="${selectorEscape(label)}"]`)
}

export function findFootnoteRef(root: HTMLElement, label: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`sup[data-fn="${selectorEscape(label)}"]`)
}

/**
 * 行内脚注引用(sup 原子节点)。输入 `[^n]` 或斜杠菜单「脚注」插入,
 * attrs id/label; 点击跳转到页面底部的对应条目(条目由 Footnotes 区块承载)。
 * 序列化为 `[^label]`, 重开页面时由 markdown-it 插件还原。
 */
export const FootnoteRef = Node.create({
  name: 'footnoteRef',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      id: {
        default: '',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-fn-id') || '',
        renderHTML: (attrs: Record<string, any>) => (attrs.id ? { 'data-fn-id': String(attrs.id) } : {}),
      },
      label: {
        default: '1',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-fn') || '1',
        renderHTML: (attrs: Record<string, any>) => ({ 'data-fn': String(attrs.label ?? '') }),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'sup[data-fn]' }]
  },

  renderHTML({ node, HTMLAttributes }) {
    return [
      'sup',
      mergeAttributes(HTMLAttributes, { class: 'fn-ref' }),
      `[^${String(node.attrs.label ?? '')}]`,
    ]
  },

  addInputRules() {
    return [
      nodeInputRule({
        find: FOOTNOTE_REF_INPUT_RE,
        type: this.type,
        getAttributes: (match: RegExpMatchArray) => ({
          id: `fn-ref-${match[2]}`,
          label: match[2],
        }),
      }),
    ]
  },

  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey('footnoteRefJump'),
        props: {
          handleClick(view, _pos, event) {
            const target = event.target as HTMLElement | null
            const sup = target?.closest('sup[data-fn]') as HTMLElement | null
            if (!sup || !view.dom.contains(sup)) return false
            flashAndScroll(findFootnoteItem(view.dom, sup.getAttribute('data-fn') || ''))
            return true
          },
        },
      }),
    ]
  },

  addStorage() {
    return {
      markdown: {
        serialize: serializeFootnoteRefMarkdown,
        parse: { setup: installMarkdownFootnotes },
      },
    }
  },
})

/**
 * 单条脚注条目(行内内容)。label 为显示编号([^label]);
 * 序列化时由父级 Footnotes 统一输出 `[^label]: 内容` 定义行。
 */
export const FootnoteItem = Node.create({
  name: 'footnoteItem',
  content: 'inline*',
  defining: true,

  addAttributes() {
    return {
      label: {
        default: '1',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-footnote') || '1',
        renderHTML: (attrs: Record<string, any>) => ({ 'data-footnote': String(attrs.label ?? '') }),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'div[data-footnote]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes(HTMLAttributes, { class: 'footnote-item' }), 0]
  },
})

/**
 * 页面底部脚注区块(容器, content: footnoteItem+)。列出 `[^n] 内容` 且可编辑;
 * 删除引用不会自动清理条目(需手动删除, 编辑器界面有提示)。
 * 序列化为若干 `[^label]: 内容` 定义行。
 */
export const Footnotes = Node.create({
  name: 'footnotes',
  group: 'block',
  content: 'footnoteItem+',
  defining: true,

  parseHTML() {
    return [{ tag: 'div[data-footnotes]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes(HTMLAttributes, { class: 'footnotes-block' }), 0]
  },

  addStorage() {
    return {
      markdown: {
        serialize: serializeFootnotesMarkdown,
        parse: { setup: installMarkdownFootnotes },
      },
    }
  },
})
