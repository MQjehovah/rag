declare module 'lowlight' {
  export function createLowlight(grammars?: any): any
}

declare module 'highlight.js/lib/languages/*' {
  const language: any
  export default language
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
