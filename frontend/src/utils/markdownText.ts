/**
 * 文本节点 → Markdown 序列化(替代 tiptap-markdown 默认实现)。
 *
 * 默认实现会按 prosemirror-markdown 的 esc 规则转义 `` ` `` `*` `\` `~` `[` `]` `_`,
 * 导致编辑器写入的 wiki 链接文本 `[[页面#标题]]` 被存成 `\[\[页面#标题\]\]`,
 * 破坏「[[ ]] 反向链接 / 渲染」的既有约定(服务端按字面量 LIKE '%[[标题]]%' 查询)。
 *
 * 本实现:
 * - 保留 `[[...]]`(不含换行/方括号)原样输出, 链接内部按常规规则转义;
 * - 其余文本沿用 esc 规则(含行首 `1. ` / `# ` / `- ` 等块级歧义转义);
 * - 行内代码(code mark)内的文本原样输出(代码内容是字面量, 转义/实体在反引号内不还原);
 * - 非代码文本的 `<`/`>` 仍转义为实体(html 模式下防止被当作 HTML 解析)。
 *
 * 纯函数(不依赖 DOM), 供 tiptap-markdown 的 Text 扩展与单测共用。
 */

const MARKDOWN_ESCAPE_RE = /[`*\\~\[\]_]/g

/** prosemirror-markdown 的字符转义(含 `_` 词内不转义规则)。 */
function escapeChars(str: string): string {
  return str.replace(MARKDOWN_ESCAPE_RE, (m, i: number) => {
    if (
      m === '_' && i > 0 && i + 1 < str.length
      && /\w/.test(str[i - 1]) && /\w/.test(str[i + 1])
    ) return m
    return '\\' + m
  })
}

/** 行首歧义转义(列表/标题/有序列表标记), 与 prosemirror-markdown 的 esc(str, startOfLine) 一致。 */
function escapeStartOfLine(str: string): string {
  return str
    .replace(/^(\+[ ]|[\-*>])/, '\\$&')
    .replace(/^(\s*)(#{1,6})(\s|$)/, '$1\\$2$3')
    .replace(/^(\s*\d+)\.\s/, '$1\\. ')
}

/** HTML 尖括号转义(html 模式下 `<x>` 不能被当作原始 HTML)。 */
function escapeHtmlAngles(str: string): string {
  return str.replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** wiki 链接 `[[标题]]`(标题内不含方括号/换行)。 */
const WIKI_LINK_RE = /\[\[[^\[\]\n]+\]\]/g

/**
 * `[[x]]` 豁免转义的边界判定: 前一字符是 `!`(会被 Markdown 重开为图片)或后一字符是 `(`(重开为链接)
 * 时不豁免, 交回普通转义。用索引手工判断而非正则 lookbehind(Safari < 16.4 不支持后行断言)。
 */
function wikiLinkExempt(html: string, start: number, end: number): boolean {
  if (start > 0 && html[start - 1] === '!') return false
  if (end < html.length && html[end] === '(') return false
  return true
}

/**
 * 文本 → Markdown 片段:
 * - `startOfLine` 为该文本是否位于行首(会影响列表/标题标记转义);
 * - code=true 时跳过 Markdown 转义(行内代码内容按字面量保存);
 * - `[[x]]` 仅在不是 `![[x]]` / `[[x]](y)` 这类会被重开为图片/链接的边界才保留字面量。
 */
export function escapeMarkdownText(text: string, startOfLine: boolean, code = false): string {
  // 行内代码内容按字面量保存: 反引号内的转义/实体都不会被还原, 必须原样输出
  if (code) return text
  const html = escapeHtmlAngles(text)
  let out = ''
  let last = 0
  html.replace(WIKI_LINK_RE, (match, idx: number) => {
    const end = idx + match.length
    if (!wikiLinkExempt(html, idx, end)) return match
    out += escapeChars(html.slice(last, idx))
    out += '[[' + escapeChars(match.slice(2, -2)) + ']]'
    last = end
    return match
  })
  out += escapeChars(html.slice(last))
  return startOfLine ? escapeStartOfLine(out) : out
}

/** tiptap-markdown 文本节点 serializer。 */
export function serializeTextMarkdown(state: any, node: any): void {
  const code = node.marks?.some?.((m: any) => m.type.name === 'code') ?? false
  state.text(escapeMarkdownText(String(node.text ?? ''), state.atBlockStart, code), false)
}
