/**
 * 单元格底色(R4a): 8 色主题安全盘 + 属性读写/净化。
 *
 * 底色以具体色值存入 `backgroundColor` 属性(非 CSS 变量), 保证 Markdown HTML
 * 兜底里的 `style="background-color: ..."` 在 Wiki/导出/剪贴板等编辑器之外
 * 也能渲染; 深色主题由全局样式强制彩色单元格内为深色文字(见 styles/global.css)。
 *
 * 纯函数(仅 `readCellBackground` 接触 element, 不依赖布局), 供编辑扩展、
 * 粘贴净化与自测共用。
 */

/** 调色板: 浅色底 + 深色文字(Notion 系中性色), 深色主题下同样安全。 */
export const CELL_BG_PALETTE = [
  { label: '灰', value: '#EBEBEA' },
  { label: '红', value: '#FBE4E4' },
  { label: '橙', value: '#FADEC9' },
  { label: '黄', value: '#FBF3DB' },
  { label: '绿', value: '#DBEDDB' },
  { label: '蓝', value: '#D3E5EF' },
  { label: '紫', value: '#E8DEEE' },
  { label: '粉', value: '#F5E0E9' },
] as const

const HEX_COLOR_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i
const RGB_COLOR_RE = /^rgba?\(\s*[\d\s.,%/]+\)$/i
const HSL_COLOR_RE = /^hsla?\(\s*[\d\s.,%/]+\)$/i
const NAMED_COLOR_RE = /^[a-z]{3,20}$/i

/**
 * 底色值安全校验: 仅接受 hex / rgb(a) / hsl(a) / 颜色名, 拒绝 `url(...)`、
 * 引号、分号等可构造 CSS 注入的值(粘贴净化与 parseHTML 共用)。
 */
export function isSafeCellBackground(value: unknown): value is string {
  const v = String(value ?? '').trim()
  if (!v || v.length > 64 || /[;'"<>\\]/.test(v)) return false
  return HEX_COLOR_RE.test(v) || RGB_COLOR_RE.test(v) || HSL_COLOR_RE.test(v) || NAMED_COLOR_RE.test(v)
}

/** 从 style 文本中提取安全的 `background-color`(供粘贴净化/parseHTML 使用)。 */
export function extractSafeBackgroundFromStyle(style: string): string | null {
  const m = /background-color\s*:\s*([^;]+)/i.exec(String(style || ''))
  const value = m?.[1]?.trim()
  return value && isSafeCellBackground(value) ? value : null
}

/**
 * 读取单元格 DOM 上的底色: `data-bg`(粘贴净化后的载体)优先, 其次 style 属性,
 * 最后回退到 style.backgroundColor(经浏览器归一化, 如 `rgb(...)`)。
 */
export function readCellBackground(el: HTMLElement): string | null {
  const dataBg = el.getAttribute('data-bg')
  if (dataBg && isSafeCellBackground(dataBg)) return dataBg.trim()
  const fromStyle = extractSafeBackgroundFromStyle(el.getAttribute('style') || '')
  if (fromStyle) return fromStyle
  const inline = el.style?.backgroundColor
  return inline && isSafeCellBackground(inline) ? inline.trim() : null
}

/** 属性 → HTML attrs: 有色输出 style, 无色不输出(保持单元格无 style 属性)。 */
export function cellBackgroundRenderAttrs(value: unknown): Record<string, string> {
  return isSafeCellBackground(value) ? { style: `background-color: ${String(value).trim()}` } : {}
}

/**
 * 颜色归一化(比较用): 浏览器经 style.cssText 会把 hex 归一成 `rgb(r, g, b)`,
 * Markdown 往返后属性值随之变化, 菜单高亮比较需要归一。
 */
export function normalizeCellColor(value: unknown): string | null {
  const v = String(value ?? '').trim().toLowerCase()
  if (!v) return null
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(v)
  if (hex) {
    const full = hex[1].length === 3 ? hex[1].split('').map((c) => c + c).join('') : hex[1]
    const n = parseInt(full, 16)
    return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`
  }
  return v
}

/** 底色是否等价(hex/rgb 归一后比较; 菜单调色板高亮用)。 */
export function cellColorsEqual(a: unknown, b: unknown): boolean {
  const na = normalizeCellColor(a)
  const nb = normalizeCellColor(b)
  return !!na && na === nb
}
