import { Node, mergeAttributes } from '@tiptap/core'

import { formatBytes } from '../utils/attachment'

export const CALLOUT_TYPES = ['info', 'tip', 'success', 'warn', 'danger'] as const
export type CalloutType = typeof CALLOUT_TYPES[number]

const escapeHtmlAttr = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

const escapeHtmlText = (text: string): string =>
  text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

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

/**
 * 块缩进容器(段落/标题/引用等顶层块). 手柄菜单「缩进/减少缩进」或 Tab/Shift+Tab
 * 将块包进该节点, attr.indent(1..4) 控制 padding-left; 序列化走 tiptap-markdown
 * 的 HTML 兜底(与 callout 相同机制): `<div data-indent="2" class="...">…</div>`,
 * 重新打开时由 parseHTML 还原, 保证 Markdown 往返不丢缩进。
 */
export const IndentBlock = Node.create({
  name: 'indentBlock',
  group: 'block',
  content: 'block+',
  defining: true,

  addAttributes() {
    return {
      indent: {
        default: 1,
        parseHTML: (el: HTMLElement) => {
          const n = parseInt(el.getAttribute('data-indent') || '1', 10) || 1
          return Math.max(1, Math.min(4, n))
        },
        renderHTML: (attrs: Record<string, any>) => {
          const n = Math.max(1, Math.min(4, Number(attrs.indent) || 1))
          return { 'data-indent': String(n), class: `indent-block indent-${n}` }
        },
      },
    }
  },

  parseHTML() {
    return [{ tag: 'div[data-indent]' }]
  },

  renderHTML({ HTMLAttributes }) {
    return ['div', mergeAttributes(HTMLAttributes), 0]
  },
})

/**
 * 非图片附件卡片(块级原子节点)。序列化为 HTML 兜底
 * `<div data-attachment data-url data-name data-size data-mime><a>…</a></div>`,
 * 在编辑器(html:true)与 Wiki(html:true + DOMPurify)中均可解析/渲染;
 * 编辑器内由 AttachmentNodeView 渲染成卡片。
 */
export const Attachment = Node.create({
  name: 'attachment',
  group: 'block',
  atom: true,
  draggable: true,
  selectable: true,

  addAttributes() {
    return {
      url: {
        default: '',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-url') || '',
        renderHTML: (attrs: Record<string, any>) => (attrs.url ? { 'data-url': attrs.url } : {}),
      },
      name: {
        default: '',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-name') || '',
        renderHTML: (attrs: Record<string, any>) => ({ 'data-name': attrs.name || '' }),
      },
      size: {
        default: 0,
        parseHTML: (el: HTMLElement) => parseInt(el.getAttribute('data-size') || '0', 10) || 0,
        renderHTML: (attrs: Record<string, any>) => ({ 'data-size': String(attrs.size || 0) }),
      },
      mime: {
        default: '',
        parseHTML: (el: HTMLElement) => el.getAttribute('data-mime') || '',
        renderHTML: (attrs: Record<string, any>) => ({ 'data-mime': attrs.mime || '' }),
      },
      // 仅编辑器运行时使用的临时属性(上传中占位),不参与序列化
      uploading: {
        default: false,
        parseHTML: () => false,
        renderHTML: () => ({}),
      },
      uploadId: {
        default: '',
        parseHTML: () => '',
        renderHTML: () => ({}),
      },
    }
  },

  parseHTML() {
    return [{ tag: 'div[data-attachment]' }]
  },

  renderHTML({ node, HTMLAttributes }) {
    const { url, name, size } = node.attrs
    const label = escapeHtmlText(name || url || '附件')
    const sizeText = size ? ` · ${formatBytes(Number(size))}` : ''
    return [
      'div',
      mergeAttributes(HTMLAttributes, { 'data-attachment': '' }),
      ['a', { href: url }, `📎 ${label}`],
      sizeText,
    ]
  },

  addStorage() {
    return {
      markdown: {
        // HTML 兜底:Markdown 里保留完整 data-* 属性,重开页面时 parseHTML 还原成卡片;
        // 纯文本查看/导出时退化为可读链接。
        serialize(state: any, node: any) {
          const url = String(node.attrs.url || '')
          const name = String(node.attrs.name || '')
          const size = Number(node.attrs.size) || 0
          const mime = String(node.attrs.mime || '')
          const attrText = [
            'data-attachment=""',
            `data-url="${escapeHtmlAttr(url)}"`,
            `data-name="${escapeHtmlAttr(name)}"`,
            `data-size="${size}"`,
            `data-mime="${escapeHtmlAttr(mime)}"`,
          ].join(' ')
          const sizeText = size ? ` · ${formatBytes(size)}` : ''
          state.write(`<div ${attrText}><a href="${escapeHtmlAttr(url)}">📎 ${escapeHtmlText(name || url || '附件')}</a>${sizeText}</div>`)
          state.closeBlock(node)
        },
      },
    }
  },
})
