export interface FindMatch {
  from: number
  to: number
}

export interface FindOptions {
  /** 区分大小写(默认 false) */
  caseSensitive?: boolean
  /**
   * 全词匹配: 仅对查询词边缘的 ASCII 单词字符追加词边界;
   * 中文等非 ASCII 边缘不追加边界, 退化为子串匹配(需求约定)。
   */
  wholeWord?: boolean
  /** 查询按正则表达式解析(默认 false) */
  regex?: boolean
}

interface TextNodeLike {
  isText?: boolean
  text?: string | null
}

interface DocLike {
  descendants: (cb: (node: TextNodeLike, pos: number) => void) => void
}

const WORD_CHAR = /[0-9A-Za-z_]/

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * 组装匹配用正则(带 g 标志; 默认忽略大小写):
 * - regex=false 时查询按字面量转义;
 * - wholeWord 只作用于"查询边缘是 ASCII 单词字符"的一侧(中文退化为子串);
 * - 非法正则返回 null。
 */
export function buildFindRegExp(query: string, options: FindOptions = {}): RegExp | null {
  const raw = (query || '').trim()
  if (!raw) return null
  const flags = options.caseSensitive ? 'g' : 'gi'
  let source = options.regex ? raw : escapeRegExp(raw)
  if (options.wholeWord && source) {
    if (WORD_CHAR.test(source[0])) source = `(?<![0-9A-Za-z_])${source}`
    if (WORD_CHAR.test(source[source.length - 1])) source = `${source}(?![0-9A-Za-z_])`
  }
  try {
    return new RegExp(source, flags)
  } catch {
    return null
  }
}

/**
 * 校验查询: 仅正则模式可能出错, 返回错误文案供 UI 提示/禁用; 合法或无查询时返回 null。
 */
export function findQueryError(query: string, options: FindOptions = {}): string | null {
  if (!options.regex) return null
  const raw = (query || '').trim()
  if (!raw) return null
  return buildFindRegExp(raw, options) ? null : '正则表达式无效'
}

/**
 * 在文档的所有文本节点内查找。不假设跨节点文本连续:
 * 命中范围保证落在单个文本节点内。文本节点从 pos 开始, 字符 i 的位置为 pos + i。
 * 零宽命中(如正则 `a*`)跳过, 避免死循环与空装饰。
 */
export function findMatchesInDoc(doc: DocLike, query: string, options: FindOptions = {}): FindMatch[] {
  const out: FindMatch[] = []
  const re = buildFindRegExp(query, options)
  if (!re) return out
  doc.descendants((node, pos) => {
    if (!node.isText || !node.text) return
    re.lastIndex = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(node.text)) !== null) {
      if (!m[0]) {
        re.lastIndex += 1
        continue
      }
      out.push({ from: pos + m.index, to: pos + m.index + m[0].length })
    }
  })
  return out
}
