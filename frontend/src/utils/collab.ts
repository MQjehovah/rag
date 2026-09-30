/** 协同编辑开关、WebSocket 探活(失败回退单人编辑)与单点播种认领决策。 */

export interface CollabPeer {
  name: string
  color: string
}

/** 播种认领(房间 meta map 上的 seedClaim): 只有认领者负责把 Markdown 播进空房间。 */
export interface SeedClaim {
  by: number
  at: number
}

/** 房间 meta(Y.Map<'meta'>)快照, 结构见 TipTapEditor 的 meta 读写。 */
export interface CollabMetaSnapshot {
  seedClaim?: SeedClaim | null
  seedDone?: boolean
  baseUpdatedAt?: string | null
}

export type SeedDecision = 'seed' | 'wait' | 'skip'

/** 校验并归一化 meta.seedClaim(容忍历史/脏数据)。 */
export function readSeedClaim(raw: unknown): SeedClaim | null {
  if (!raw || typeof raw !== 'object') return null
  const by = Number((raw as { by?: unknown }).by)
  const at = Number((raw as { at?: unknown }).at)
  if (!Number.isFinite(by)) return null
  return { by, at: Number.isFinite(at) ? at : 0 }
}

/**
 * 单点播种决策(纯函数): sync 完成、写入自己的 seedClaim 并等一个同步回合后复查。
 * - 文档已有内容(对端已播 / y-indexeddb 恢复) → skip;
 * - 房间已播种过(seedDone, 即便内容被清空) → skip;
 * - 认领者是自己且文档为空 → seed;
 * - 认领者是别人 → wait(等待其播种);
 * - 无认领(遗留房间/异常) → seed 兜底(调用方已先写入自己的认领)。
 */
export function decideSeed(
  meta: CollabMetaSnapshot | null | undefined,
  clientID: number,
  fragmentEmpty: boolean,
): SeedDecision {
  if (!fragmentEmpty) return 'skip'
  if (meta?.seedDone) return 'skip'
  const claim = meta?.seedClaim
  if (claim && Number.isFinite(claim.by)) return claim.by === clientID ? 'seed' : 'wait'
  return 'seed'
}

/**
 * 是否启用 y-indexeddb 离线持久化(纯函数, 保守):
 * 协同打开且拿到页面 updated_at(陈旧检测的基准)才启用; 缺失时禁用。
 */
export function isPersistenceEnabled(
  collabEnabled: boolean,
  pageUpdatedAt: string | null | undefined,
): boolean {
  return collabEnabled && String(pageUpdatedAt ?? '').trim() !== ''
}

/**
 * 持久化恢复的基准与当前页面 updated_at 是否不一致(纯函数):
 * 任一缺失 → false(无法判定, 不清库); 不一致 → true(服务端页面已变更, 清库重播)。
 */
export function isPersistedBaseStale(
  persistedBaseUpdatedAt: string | null | undefined,
  currentPageUpdatedAt: string | null | undefined,
): boolean {
  const base = String(persistedBaseUpdatedAt ?? '').trim()
  const current = String(currentPageUpdatedAt ?? '').trim()
  if (!base || !current) return false
  return base !== current
}

/**
 * 协同播种编辑闸门(纯函数, F1-1): 播种完成(seedDone)前禁止编辑与 emit。
 * 未播种期用户输入若被 emit, 会触发协作不带 base 的自动保存, 把"只含新输入"的
 * Markdown 整页覆盖服务端; 回退单人(非协同)与播种完成均放行。
 */
export function collabEditGate(
  collab: boolean,
  seedDone: boolean,
): { editable: boolean; emitUpdate: boolean } {
  if (!collab) return { editable: true, emitUpdate: true }
  return { editable: seedDone, emitUpdate: seedDone }
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
