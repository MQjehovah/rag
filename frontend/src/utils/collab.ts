/** 协同编辑开关与 WebSocket 探活(失败回退单人编辑)。 */

export interface CollabPeer {
  name: string
  color: string
}

/** 探活结果缓存: 成功缓存整个会话(页面内 4s 无同步还有兜底), 失败缓存 30s 后允许重试。 */
const PROBE_FAIL_TTL = 30_000
let probeCache: { ok: boolean; at: number } | null = null

export function getCachedCollabProbe(now = Date.now()): boolean | null {
  if (!probeCache) return null
  if (probeCache.ok) return true
  return now - probeCache.at < PROBE_FAIL_TTL ? false : null
}

export function setCachedCollabProbe(ok: boolean, now = Date.now()): void {
  probeCache = { ok, at: now }
}

export function resetCachedCollabProbe(): void {
  probeCache = null
}

/**
 * 协同功能是否启用(默认开启):
 * - `VITE_RAG_COLLAB_ENABLED=false|0` 关闭(构建期);
 * - URL 查询参数 `?collab=0|false|off` 关闭 / `?collab=1|true|on` 强制开启(运行期一键退回)。
 */
export function isCollabFeatureEnabled(opts: { query?: string; envValue?: string } = {}): boolean {
  const query = opts.query ?? ''
  const param = new URLSearchParams(query).get('collab')
  if (param !== null) {
    const v = param.toLowerCase()
    if (v === '0' || v === 'false' || v === 'off' || v === 'no') return false
    if (v === '1' || v === 'true' || v === 'on' || v === 'yes') return true
  }
  const env = String(opts.envValue ?? '').toLowerCase()
  return !(env === 'false' || env === '0' || env === 'off')
}

/** 用一个临时 WebSocket 探测协作服务可达性; 不依赖 Yjs, 打开即算成功。 */
export function probeCollabServer(url: string, timeoutMs = 1500): Promise<boolean> {
  return new Promise(resolve => {
    let settled = false
    let ws: WebSocket | null = null
    const finish = (ok: boolean) => {
      if (settled) return
      settled = true
      try { ws?.close() } catch { /* ignore */ }
      resolve(ok)
    }
    const timer = setTimeout(() => finish(false), timeoutMs)
    try {
      ws = new WebSocket(`${url.replace(/\/$/, '')}/__collab_probe__`)
      ws.onopen = () => { clearTimeout(timer); finish(true) }
      ws.onerror = () => { clearTimeout(timer); finish(false) }
      ws.onclose = () => { clearTimeout(timer); finish(false) }
    } catch {
      clearTimeout(timer)
      finish(false)
    }
  })
}
