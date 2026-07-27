declare module 'lowlight' {
  export function createLowlight(options: any): any
  export const all: any
  export const common: any
}

declare module '@tiptap/extension-code-block-lowlight' {
  const extension: any
  export default extension
}

declare module 'tiptap-markdown' {
  export const Markdown: any
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}