import { Node, mergeAttributes } from '@tiptap/core'

export const CALLOUT_TYPES = ['info', 'tip', 'success', 'warn', 'danger'] as const
export type CalloutType = typeof CALLOUT_TYPES[number]

/**
 * 提示框(callout)块。序列化走 tiptap-markdown 的 HTML 回退
 * (`<div data-callout="info">...</div>`), 在编辑器(html:true)与
 * Wiki(html:true + DOMPurify)中均可解析/渲染。
 */
export const Callout = Node.create({
  name: 'callout',
  group: 'block',
  content: 'block+',
  defining: true,

  addAttributes() {
    return {
      type: {
        default: 'info',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-callout') || 'info',
        renderHTML: (attrs: Record<string, any>) => ({ 'data-callout': attrs.type }),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'div[data-callout]' }]
  },

  renderHTML({ node, HTMLAttributes }) {
    return [
      'div',
      mergeAttributes(HTMLAttributes, { class: `callout callout-${node.attrs.type}` }),
      0,
    ]
  },
})

/**
 * 折叠块(toggle)。标题存于 `title` 属性, 正文为块级内容; 序列化为
 * `<details data-toggle open><summary>标题</summary><div class="toggle-content">…</div></details>`。
 */
export const Toggle = Node.create({
  name: 'toggle',
  group: 'block',
  content: 'block+',
  defining: true,

  addAttributes() {
    return {
      open: {
        default: true,
        parseHTML: (el: HTMLElement) => el.hasAttribute('open'),
        renderHTML: (attrs: Record<string, any>) => (attrs.open ? { open: '' } : {}),
      },
      title: {
        default: '',
        parseHTML: (el: HTMLElement) => el.querySelector('summary')?.textContent || '',
        renderHTML: () => ({}),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'details[data-toggle]', contentElement: 'div.toggle-content' }]
  },

  renderHTML({ node, HTMLAttributes }) {
    return [
      'details',
      mergeAttributes(HTMLAttributes, { 'data-toggle': '' }),
      ['summary', {}, node.attrs.title || '折叠块'],
      ['div', { class: 'toggle-content' }, 0],
    ]
  },
})
