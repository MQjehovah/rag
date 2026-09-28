/** 提取后端错误文案:FastAPI 的 detail 可能是字符串或 422 校验数组;其余情况用兜底文案。 */
export function errText(e: unknown, fallback = '操作失败'): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } } | undefined)?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const msgs = detail
      .map(item => {
        if (typeof item === 'object' && item !== null && typeof (item as { msg?: unknown }).msg === 'string') {
          return (item as { msg: string }).msg
        }
        return ''
      })
      .filter(Boolean)
    if (msgs.length) return msgs.join('；')
  }
  return fallback
}
