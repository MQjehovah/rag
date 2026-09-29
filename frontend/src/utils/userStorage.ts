import { useAuthStore } from '../stores/auth'

/**
 * 给「用户内容」类 localStorage key 加账号作用域。
 * 未登录/用户信息未就绪时归到 `anon`，绝不回落到无前缀的全局 key，避免换号串数据。
 */
export function scopedKey(base: string): string {
  let uid = 'anon'
  try {
    uid = useAuthStore().user?.id || 'anon'
  } catch {
    // pinia 未激活等异常场景, 仍返回 anon 前缀, 不暴露全局 key
  }
  return `${base}:${uid}`
}

/** 读取当前账号作用域下的 JSON；缺失、解析失败或存储不可用时返回 fallback。 */
export function scopedGetJSON<T>(base: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(scopedKey(base))
    if (!raw) return fallback
    return JSON.parse(raw) as T
  } catch {
    return fallback
  }
}

/** 写入当前账号作用域下的 JSON；存储不可用时静默失败。 */
export function scopedSetJSON(base: string, value: unknown): void {
  try {
    localStorage.setItem(scopedKey(base), JSON.stringify(value))
  } catch {
    // storage full / unavailable
  }
}
