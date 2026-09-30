/**
 * 打印兜底开关: 打印/导出 PDF 前由 TipTapEditor 置 true, 惰渲染图片(ImageNodeView)
 * 据此忽略视口判定立即加载真实图片, 打印结束后置回 false 恢复惰加载。
 *
 * 用模块级 ref 而非 provide/inject: tiptap 的 Vue NodeView 经独立 render 挂载,
 * 不在 TipTapEditor 的组件树里, inject 拿不到组件级 provide 的值。
 */
import { ref } from 'vue'

export const printExpand = ref(false)
