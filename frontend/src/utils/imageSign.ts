import http from '../api/http'

// 子路径部署时页面挂在 BASE_URL(如 /rag/)。签名接口经 http 实例自带 baseURL,
// 但写回 <img src>/<a href> 的地址必须显式带前缀;发给后端签名的路径保持裸路径。
const API_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')

const PROXY_PATH = '/api/upload/images/proxy?url='
const LOCAL_PREFIX = '/api/upload/images/'
const SIGN_ENDPOINT = '/api/upload/images/sign'

const withBase = (path: string): string => (path.startsWith('/') ? API_BASE + path : path)

/** 从已归一化的代理路径里取出被代理的原始地址；缺失或解码失败返回 null。 */
const proxyTarget = (path: string): string | null => {
  const query = path.slice(path.indexOf('?') + 1)
  try {
    return new URLSearchParams(query).get('url') || null
  } catch {
    return null
  }
}

/**
 * 把 <img> 的原始地址映射为需要后端签名的地址：
 * - 同源的本地 `/api/upload/images/...` 路径：直接签名；
 * - 跨域外链、或已归一化的代理路径：改写成只含「内层目标」的代理路径后再签名，
 *   避免把本站代理路径再次代理（proxy-of-proxy）或把本站地址当外链代理；
 * - data URI、已带 sig= 的地址、其它相对路径：不处理，返回 null。
 */
export const mapImageSrc = (src: string): string | null => {
  if (!src || /^data:/i.test(src) || /[?&]sig=/.test(src)) return null

  let candidate = src
  if (/^https?:/i.test(src)) {
    let parsed: URL | null = null
    try {
      parsed = new URL(src)
    } catch {
      parsed = null
    }
    if (parsed && parsed.origin === window.location.origin) {
      // 同源绝对地址先还原成路径，再按本地/代理规则分类，绝不当外链代理。
      candidate = parsed.pathname + parsed.search
    } else {
      return PROXY_PATH + encodeURIComponent(src)
    }
  }

  // 后端生成的地址可能自带子路径前缀(如 /rag/api/...);签名对象始终是不含前缀的裸路径。
  if (API_BASE && (candidate === API_BASE || candidate.startsWith(API_BASE + '/'))) {
    candidate = candidate.slice(API_BASE.length) || '/'
  }

  if (candidate.startsWith(LOCAL_PREFIX) && !candidate.startsWith(PROXY_PATH)) {
    return candidate
  }
  if (candidate.startsWith(PROXY_PATH)) {
    const target = proxyTarget(candidate)
    if (!target) return null
    return PROXY_PATH + encodeURIComponent(target)
  }
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

// 已尝试签名的元素：防止 @error 回退路径对同一张图反复签名形成循环。
const attempted = new WeakSet<HTMLImageElement>()

// 串行队列 + 在途标记：避免多个 apply 并发 POST，也不丢弃后到的签名请求。
let pending: { el: HTMLImageElement; url: string }[] = []
let running = false

const drain = async () => {
  if (running) return
  running = true
  try {
    while (pending.length) {
      const batch = pending
      pending = []
      try {
        const res = await http.post<{ urls: string[] }>(SIGN_ENDPOINT, { urls: batch.map((b) => b.url) })
        const signed = res.data?.urls || []
        batch.forEach((item, i) => {
          const next = signed[i]
          if (!next) return
          attempted.add(item.el)
          item.el.src = withBase(next)
          const link = item.el.closest('a')
          if (link) link.href = withBase(next)
        })
      } catch (e) {
        // 签名失败时保留原始 src，不影响其余渲染，也不弹窗打扰用户。
        console.warn('[imageSign] 图片签名失败，保留原始地址', e)
      }
    }
  } finally {
    running = false
  }
}

const enqueue = (targets: HTMLImageElement[], urls: string[]) => {
  targets.forEach((el, i) => {
    if (urls[i]) pending.push({ el, url: urls[i] })
  })
  void drain()
}

/** 扫描容器内(或给定列表)的图片，换取签名后写回 src。 */
export const signRenderedImages = (source: HTMLElement | Iterable<HTMLImageElement>): void => {
  const { targets, urls } = collect(source)
  enqueue(targets, urls)
}

/**
 * 单张图片签名重试（用于加载失败的回退路径，如懒渲染的 tooltip）。
 * 返回 true 表示已排入签名；返回 false 表示无需签名或已尝试过。
 */
export const signImageElement = (el: HTMLImageElement): boolean => {
  if (attempted.has(el)) return false
  const mapped = mapImageSrc(el.getAttribute('src') || '')
  if (!mapped) return false
  attempted.add(el)
  enqueue([el], [mapped])
  return true
}
