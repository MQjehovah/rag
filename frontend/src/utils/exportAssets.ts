/**
 * 导出资源内联: 把页面里的图片尽力转成 base64 内嵌(供 Word/PDF 导出使用)。
 *
 * - 本地/代理图片经现有签名接口(`/api/upload/images/sign`)换签名后用带 token 的
 *   axios 拉取, 复用后端既有鉴权, 不新开绕权通道;
 * - 跨域外链直接 fetch(可能被 CORS 拒绝), 失败即跳过;
 * - 单张失败从 HTML 中移除该 <img>, 不阻断整体导出。
 */
import http from '../api/http'
import { mapImageSrc } from './imageSign'
import { mapLimit } from './pageArchive'

const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
const SIGN_ENDPOINT = '/api/upload/images/sign'

const stripBase = (path: string): string =>
  API_BASE && path.startsWith(API_BASE + '/') ? path.slice(API_BASE.length) : path

const blobToDataUrl = (blob: Blob) => new Promise<string>((resolve, reject) => {
  const reader = new FileReader()
  reader.onload = () => resolve(String(reader.result || ''))
  reader.onerror = () => reject(reader.error || new Error('读取图片失败'))
  reader.readAsDataURL(blob)
})

const isDataUrl = (src: string) => /^data:/i.test(src)

/** 单张图片 → data:URL;需要签名/鉴权的先换签名;失败返回 null。 */
export async function fetchImageAsDataUrl(src: string, timeoutMs = 20000): Promise<string | null> {
  const raw = String(src || '').trim()
  if (!raw) return null
  if (isDataUrl(raw)) return raw

  let path: string | null = null
  const mapped = mapImageSrc(raw)
  if (mapped) {
    // 本地存储/外链代理路径: 先经签名接口换带签名的访问地址
    try {
      const res = await http.post<{ urls: string[] }>(
        SIGN_ENDPOINT, { urls: [mapped] }, { timeout: timeoutMs },
      )
      const signed = res.data?.urls?.[0]
      path = signed ? stripBase(signed) : null
    } catch {
      path = null
    }
  } else if (/^https?:/i.test(raw)) {
    let parsed: URL | null = null
    try {
      parsed = new URL(raw)
    } catch {
      return null
    }
    if (parsed.origin === window.location.origin) {
      path = stripBase(parsed.pathname + parsed.search)
    } else {
      try {
        const resp = await fetch(raw, { mode: 'cors', credentials: 'omit' })
        if (!resp.ok) return null
        return await blobToDataUrl(await resp.blob())
      } catch {
        return null
      }
    }
  } else {
    // 裸相对路径(如 content 里手写的 ./a.png)按站点相对处理
    path = raw
  }
  if (!path) return null
  try {
    const res = await http.get(path, { responseType: 'blob', timeout: timeoutMs })
    const blob = res.data as Blob
    if (!blob || !blob.size) return null
    return await blobToDataUrl(blob)
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
