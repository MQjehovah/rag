/**
 * 表格单元格底色扩展(R4a): 在 TableCell/TableHeader 上追加 `backgroundColor` 属性。
 *
 * - attrs: 默认 null; parseHTML 依次读 `data-bg`(粘贴净化载体)/style;
 * - renderHTML: 有色时输出 `style="background-color: ..."`, 无色不输出;
 * - Markdown 往返: 带底色的表格回退整表 HTML(见 utils/markdownTable.ts),
 *   HTML 中的 style 再次打开时由本扩展还原。
 */

import { TableCell } from '@tiptap/extension-table-cell'
import { TableHeader } from '@tiptap/extension-table-header'
import { cellBackgroundRenderAttrs, readCellBackground } from '../utils/tableCellColor'

function cellBackgroundAttribute() {
  return {
    backgroundColor: {
      default: null,
      parseHTML: (el: HTMLElement) => readCellBackground(el),
      renderHTML: (attrs: Record<string, any>) => cellBackgroundRenderAttrs(attrs.backgroundColor),
    },
  }
}

export const TableCellWithColor = TableCell.extend({
  addAttributes() {
    return {
      ...(this.parent?.() ?? {}),
      ...cellBackgroundAttribute(),
    }
  },
})

export const TableHeaderWithColor = TableHeader.extend({
  addAttributes() {
    return {
      ...(this.parent?.() ?? {}),
      ...cellBackgroundAttribute(),
    }
  },
})
