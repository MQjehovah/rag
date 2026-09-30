/**
 * 导出资源内联: 把页面里的图片尽力转成 base64 内嵌(供 Word/PDF 导出使用)。
 *
 * - 本地/代理图片经现有签名接口(`/api/upload/images/sign`)换签名后用带 token 的
 *   axios 拉取, 复用后端既有鉴权, 不新开绕权通道;
 * - 未归入签名通道的跨域地址直接 fetch(可能被 CORS 拒绝), 失败即跳过;
 * - 单张失败从 HTML 中移除该 <img>, 不阻断整体导出。
 *
 * 安全: 带 token 的 axios 仅用于**已验证的同源站内路径**(见 classifyImageSrc);
 * 跨域/协议相对地址(如 `//evil.com/x`)一律走无凭据 fetch, 防止 token 外泄。
 */
import http from '../api/http'
import { mapImageSrc } from './imageSign'
import { mapLimit, parseAttachmentUrl } from './pageArchive'

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
const SIGN_ENDPOINT = '/api/upload/images/sign'
const ATTACHMENT_PREFIX = '/api/upload/attachments/'

const stripBase = (path: string): string =>
  API_BASE && path.startsWith(API_BASE + '/') ? path.slice(API_BASE.length) : path

const blobToDataUrl = (blob: Blob) => new Promise<string>((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result || ''))
  reader.onerror = () => reject(reader.error || new Error('读取图片失败'))
  reader.readAsDataURL(blob)
})

const isDataUrl = (src: string) => /^data:/i.test(src)
const isBlobUrl = (src: string) => /^blob:/i.test(src)

/**
 * 地址按 `origin`(默认当前站点)解析为站内裸路径; 仅当同源且 pathname 不以 `//` 开头时返回,
 * 其余(跨域/协议相对/`//` 开头/非法)一律 null —— 这是带 token 请求的唯一准入判定。
 * 解析结果在去 baseURL 前缀后再次校验, 防止 `/base//evil.com` 这类残留 `//` 前缀被 axios 当作绝对 URL。
 */
export function resolveSameOriginPath(raw: string, origin = window.location.origin): string | null {
  let u: URL
  try {
    u = new URL(raw, origin)
  } catch {
    return null
  }
  if (u.origin !== origin) return null
  if (u.pathname.startsWith('//')) return null
  const path = stripBase(u.pathname + u.search)
  if (!path.startsWith('/') || path.startsWith('//')) return null
  return path
}

/** 图片地址的获取计划(纯函数, 安全分类的唯一入口)。 */
export type ImageSrcPlan =
  | { kind: 'inline'; dataUrl: string }
  | { kind: 'sign'; url: string }
  | { kind: 'path'; path: string }
  | { kind: 'remote'; url: string }
  | { kind: 'skip' }

/**
 * 分类 <img> 地址:
 * - data: 直接内联, 不请求; blob: 仅页面会话内有效, 不请求直接跳过;
 * - 需签名的本地上传/外链代理路径 → sign(签名接口本身带鉴权, 换到的地址仍须同源校验);
 * - 同源非签名路径 → path(唯一允许带 token 的 http.get 分支);
 * - 跨域/协议相对 http(s) → remote(无凭据 fetch, 可能被 CORS 拒绝);
 * - 其余(javascript:/非法地址等) → skip。
 */
export function classifyImageSrc(raw: string, origin = window.location.origin): ImageSrcPlan {
  const src = String(raw || '').trim()
  if (!src) return { kind: 'skip' }
  if (isDataUrl(src)) return { kind: 'inline', dataUrl: src }
  if (isBlobUrl(src)) return { kind: 'skip' }
  const mapped = mapImageSrc(src)
  if (mapped) return { kind: 'sign', url: mapped }
  let u: URL
  try {
    u = new URL(src, origin)
  } catch {
    return { kind: 'skip' }
  }
  if (u.origin === origin) {
    const path = resolveSameOriginPath(src, origin)
    return path ? { kind: 'path', path } : { kind: 'skip' }
  }
  if (u.protocol === 'http:' || u.protocol === 'https:') return { kind: 'remote', url: u.href }
  return { kind: 'skip' }
}

/** 带 token 的同源路径请求(仅由 classifyImageSrc/fetchAttachmentBlob 的 path/sign 分支调用)。 */
async function fetchAuthedPathAsBlob(path: string, timeoutMs: number): Promise<Blob | null> {
  try {
    const res = await http.get(path, { responseType: 'blob', timeout: timeoutMs })
    const blob = res.data as Blob
    if (!blob || !blob.size) return null
    return blob
  } catch {
    return null
  }
}

async function fetchAuthedPathAsDataUrl(path: string, timeoutMs: number): Promise<string | null> {
  const blob = await fetchAuthedPathAsBlob(path, timeoutMs)
  if (!blob) return null
  try {
    return await blobToDataUrl(blob)
  } catch {
    return null
  }
}

/** 单张图片 → data:URL;需要签名/鉴权的先换签名;失败返回 null。 */
export async function fetchImageAsDataUrl(src: string, timeoutMs = 20000): Promise<string | null> {
  const plan = classifyImageSrc(src)
  if (plan.kind === 'inline') return plan.dataUrl
  if (plan.kind === 'skip') return null
  if (plan.kind === 'remote') {
    try {
      const resp = await fetch(plan.url, { mode: 'cors', credentials: 'omit' })
      if (!resp.ok) return null
      return await blobToDataUrl(await resp.blob())
    } catch {
      return null
    }
  }
  if (plan.kind === 'path') return fetchAuthedPathAsDataUrl(plan.path, timeoutMs)
  // sign: 经签名接口换签名; 后端返回的地址必须再次同源校验, 不合格直接跳过
  try {
    const res = await http.post<{ urls: string[] }>(
      SIGN_ENDPOINT, { urls: [plan.url] }, { timeout: timeoutMs },
    )
    const signed = res.data?.urls?.[0]
    const path = signed ? resolveSameOriginPath(signed) : null
    if (!path) return null
    return await fetchAuthedPathAsDataUrl(path, timeoutMs)
  } catch {
    return null
  }
}

export interface InlineImagesResult {
  html: string
  inlined: number
  skipped: number
}

/** HTML 中的 <img> 尽力转 base64;单张失败移除该图并计数。 */
export async function inlineImagesInHtml(
  html: string,
  opts: { concurrency?: number; timeoutMs?: number } = {},
): Promise<InlineImagesResult> {
  if (!/<img[\s>]/i.test(html)) return { html, inlined: 0, skipped: 0 }
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const imgs = Array.from(doc.querySelectorAll('img'))
  let inlined = 0
  let skipped = 0
  await mapLimit(imgs, opts.concurrency ?? 4, async (img) => {
    const dataUrl = await fetchImageAsDataUrl(img.getAttribute('src') || '', opts.timeoutMs)
    if (!dataUrl) {
      skipped++
      img.remove()
      return
    }
    img.setAttribute('src', dataUrl)
    img.removeAttribute('srcset')
    inlined++
  })
  return {
    html: '<!DOCTYPE html>\n' + doc.documentElement.outerHTML,
    inlined,
    skipped,
  }
}

// ---------------- 附件(zip 导出附带) ----------------

/** 附件地址的获取计划(纯函数, 安全分类的唯一入口)。 */
export type AttachmentSrcPlan =
  | { kind: 'sign'; path: string }
  | { kind: 'path'; path: string }
  | { kind: 'remote'; url: string }
  | { kind: 'skip' }

/**
 * 分类附件地址:
 * - 非附件地址(data: 协议连图片都排除的口径一致, blob: 会话内有效但无法归档) → skip;
 * - 同源本地 `/api/upload/attachments/...` → sign(换新签名后带 token 拉取, 丢弃旧签名查询);
 * - 同源其它路径 → path(唯一允许带 token 的 http.get 分支, 复用 resolveSameOriginPath 防 token 外泄);
 * - 跨域 http(s)(如 MinIO 直链) → remote(无凭据 fetch, 可能被 CORS 拒绝);
 * - 其余 → skip。
 */
export function classifyAttachmentSrc(raw: string, origin = window.location.origin): AttachmentSrcPlan {
  const src = String(raw || '').trim()
  if (!src || isDataUrl(src) || isBlobUrl(src)) return { kind: 'skip' }
  if (!parseAttachmentUrl(src)) return { kind: 'skip' }
  let u: URL
  try {
    u = new URL(src, origin)
  } catch {
    return { kind: 'skip' }
  }
  if (u.origin === origin) {
    const bare = stripBase(u.pathname)
    if (bare.startsWith(ATTACHMENT_PREFIX)) return { kind: 'sign', path: bare }
    const path = resolveSameOriginPath(u.pathname, origin)
    return path ? { kind: 'path', path } : { kind: 'skip' }
  }
  if (u.protocol === 'http:' || u.protocol === 'https:') return { kind: 'remote', url: u.href }
  return { kind: 'skip' }
}

/** 单个附件 → Blob(复用导出图片的签名/同源鉴权/跨域无凭据规则);失败返回 null, 不抛异常。 */
export async function fetchAttachmentBlob(src: string, timeoutMs = 30000): Promise<Blob | null> {
  const plan = classifyAttachmentSrc(src)
  if (plan.kind === 'skip') return null
  if (plan.kind === 'remote') {
    try {
      const resp = await fetch(plan.url, { mode: 'cors', credentials: 'omit' })
      if (!resp.ok) return null
      const blob = await resp.blob()
      return blob.size ? blob : null
    } catch {
      return null
    }
  }
  if (plan.kind === 'path') return fetchAuthedPathAsBlob(plan.path, timeoutMs)
  // sign: 经签名接口换签名;后端返回的地址必须再次同源校验, 不合格直接跳过
  try {
    const res = await http.post<{ urls: string[] }>(
      SIGN_ENDPOINT, { urls: [plan.path] }, { timeout: timeoutMs },
    )
    const signed = res.data?.urls?.[0]
    const path = signed ? resolveSameOriginPath(signed) : null
    if (!path) return null
    return await fetchAuthedPathAsBlob(path, timeoutMs)
  } catch {
    return null
  }
}
