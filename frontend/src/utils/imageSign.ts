import http from '../api/http'

const PROXY_PATH = '/api/upload/images/proxy?url='
const LOCAL_PREFIX = '/api/upload/images/'
const SIGN_ENDPOINT = '/api/upload/images/sign'

/**
 * 把 <img> 的原始地址映射为需要后端签名的地址：
 * - 外链(绝对 http/https)：改写成无 Referer 代理路径后再签名；
 * - 已在本站的 /api/upload/images/... 路径：直接签名；
 * - data URI、已带 sig= 的地址、其它相对路径：不处理，返回 null。
 */
export const mapImageSrc = (src: string): string | null => {
  if (!src || /^data:/i.test(src) || /[?&]sig=/.test(src)) return null
  if (/^https?:/i.test(src)) return PROXY_PATH + encodeURIComponent(src)
  if (src.startsWith(LOCAL_PREFIX)) return src
  return null
}

const collect = (source: HTMLElement | Iterable<HTMLImageElement>) => {
  const elements: HTMLImageElement[] = source instanceof HTMLElement
    ? Array.from(source.querySelectorAll('img'))
    : Array.from(source)
  const targets: HTMLImageElement[] = []
  const urls: string[] = []
  for (const el of elements) {
    const mapped = mapImageSrc(el.getAttribute('src') || '')
    if (mapped) {
      targets.push(el)
      urls.push(mapped)
    }
  }
  return { targets, urls }
}

const apply = async (targets: HTMLImageElement[], urls: string[]) => {
  if (!urls.length) return
  try {
    const res = await http.post<{ urls: string[] }>(SIGN_ENDPOINT, { urls })
    const signed = res.data?.urls || []
    targets.forEach((el, i) => {
      const next = signed[i]
      if (!next) return
      el.src = next
      const link = el.closest('a')
      if (link) link.href = next
    })
  } catch (e) {
    // 签名失败时保留原始 src，不影响其余渲染，也不弹窗打扰用户。
    console.warn('[imageSign] 图片签名失败，保留原始地址', e)
  }
}

/** 扫描容器内(或给定列表)的图片，换取签名后写回 src。 */
export const signRenderedImages = (source: HTMLElement | Iterable<HTMLImageElement>): void => {
  const { targets, urls } = collect(source)
  void apply(targets, urls)
}

/** 单张图片签名重试（用于加载失败的回退路径，如懒渲染的 tooltip）。 */
export const signImageElement = (el: HTMLImageElement): void => {
  const mapped = mapImageSrc(el.getAttribute('src') || '')
  if (mapped) void apply([el], [mapped])
}
