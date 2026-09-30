import http from '../api/http'

// 子路径部署时页面挂在 BASE_URL(如 /rag/)。签名接口经 http 实例自带 baseURL,
// 但写回 <a href>/window.open 的地址必须显式带前缀;发给后端签名的路径保持裸路径。
// 附件与图片共用同一签名端点:后端识别 /api/upload/attachments/ 前缀。
const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')

const ATTACHMENT_PREFIX = '/api/upload/attachments/'
const SIGN_ENDPOINT = '/api/upload/images/sign'

const withBase = (path: string): string => (path.startsWith('/') ? API_BASE + path : path)

/** 人类可读的文件大小;0/非法值返回空串。 */
export const formatBytes = (size: number): string => {
  if (!size || size <= 0) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = size
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  const text = i === 0 ? String(value) : value >= 100 ? value.toFixed(0) : value.toFixed(1)
  return `${text} ${units[i]}`
}

export type AttachmentPreviewKind = 'image' | 'pdf' | 'none'

/** 预览类型:图片(含 svg)/PDF/不支持(仅下载)。mime 优先,回退扩展名。 */
export const attachmentPreviewKind = (mime: string, name: string): AttachmentPreviewKind => {
  const type = (mime || '').toLowerCase()
  if (type.startsWith('image/')) return 'image'
  if (type === 'application/pdf') return 'pdf'
  const ext = (name.split('.').pop() || '').toLowerCase()
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'avif', 'svg'].includes(ext)) return 'image'
  if (ext === 'pdf') return 'pdf'
  return 'none'
}

/** 判断附件是否适合内联预览(图片/PDF),否则触发下载。 */
export const canPreviewAttachment = (mime: string, name: string): boolean =>
  attachmentPreviewKind(mime, name) !== 'none'

/**
 * 把附件地址映射为可直接访问的 URL:
 * - 本地 /api/upload/attachments/... 路径:向后端换取带 sig 的签名 URL;
 * - http(s) 直链(如 MinIO)与 data/blob:原样返回;
 * - 其它未知地址:原样返回,不做处理。
 */
export const resolveAttachmentUrl = async (url: string): Promise<string> => {
  if (!url) return ''
  if (/^(https?:|data:|blob:)/i.test(url)) return url

  let candidate = url
  if (API_BASE && (candidate === API_BASE || candidate.startsWith(API_BASE + '/'))) {
    candidate = candidate.slice(API_BASE.length) || '/'
  }
  if (!candidate.startsWith(ATTACHMENT_PREFIX)) return url

  const res = await http.post<{ urls: string[] }>(SIGN_ENDPOINT, { urls: [candidate] })
  const signed = res.data?.urls?.[0]
  return signed ? withBase(signed) : url
}

/** 新窗口打开附件(图片/PDF);先同步开窗避免被浏览器拦截,再跳转。 */
export const openAttachment = async (url: string): Promise<void> => {
  const win = window.open('', '_blank')
  try {
    const href = await resolveAttachmentUrl(url)
    if (win) win.location.href = href
    else window.open(href, '_blank', 'noopener')
  } catch (e) {
    win?.close()
    throw e
  }
}

/** 签名 URL 是否已过期(带 30s 时钟偏差余量);无 exp 参数视为不过期。 */
export const isSignedUrlExpired = (url: string, skewSeconds = 30): boolean => {
  const query = url.split('?')[1] || ''
  const exp = new URLSearchParams(query).get('exp')
  if (!exp) return false
  const ts = Number(exp)
  return Number.isFinite(ts) && ts * 1000 <= Date.now() + skewSeconds * 1000
}

/**
 * 预览用地址解析:先走既有签名逻辑;若拿到的签名 URL 已过期(或已无 exp),
 * 重新签名再取一次(仅重试一次,避免死循环)。
 */
export const resolveAttachmentUrlForPreview = async (url: string): Promise<string> => {
  const href = await resolveAttachmentUrl(url)
  if (isSignedUrlExpired(href)) return resolveAttachmentUrl(url)
  return href
}

/** 触发附件下载(同源签名 URL 下 download 属性生效)。 */
export const downloadAttachment = async (url: string, name: string): Promise<void> => {
  const href = await resolveAttachmentUrl(url)
  const a = document.createElement('a')
  a.href = href
  a.download = name || ''
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}
