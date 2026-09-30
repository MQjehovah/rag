/**
 * 模板变量占位(纯函数): `{{变量名}}` 的提取与填充。
 *
 * 约定:
 * - 变量名 = 双花括号内去除首尾空白后的文本, 允许中英文/数字/下划线等, 不含花括号与换行;
 * - 同名变量只出现一次(按首次出现顺序);
 * - 填充时未提供的变量保留原占位符(便于用户生成后再编辑), 而不是清空。
 */

export const TEMPLATE_VAR_LIMIT = 100
export const TEMPLATE_VAR_NAME_MAX = 64

const variableRe = () => /\{\{\s*([^{}\n]+?)\s*\}\}/g

/** 从模板内容中提取变量名(去重, 保持首次出现顺序)。 */
export function extractTemplateVariables(content: string): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  const re = variableRe()
  let match: RegExpExecArray | null
  while ((match = re.exec(String(content || '')))) {
    const name = match[1].trim().slice(0, TEMPLATE_VAR_NAME_MAX)
    if (!name || seen.has(name)) continue
    seen.add(name)
    out.push(name)
    if (out.length >= TEMPLATE_VAR_LIMIT) break
  }
  return out
}

/** 用 values 填充变量;未提供的变量保留 `{{变量名}}` 原样。 */
export function renderTemplateContent(content: string, values: Record<string, string>): string {
  return String(content || '').replace(variableRe(), (whole, rawName: string) => {
    const name = rawName.trim().slice(0, TEMPLATE_VAR_NAME_MAX)
    const value = values[name]
    return value === undefined || value === null ? whole : value
  })
}
