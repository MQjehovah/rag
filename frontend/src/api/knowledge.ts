import http from './http'
import type { WikiResultView, RawResultView } from './ragChat'

export interface SearchV2Result {
  scope_id: string | null
  wiki_results: WikiResultView[]
  raw_results: RawResultView[]
  total: number
  degraded_reasons: string[]
}

// J-1：知识中心搜索不再要求前端提交权限域，后端自动计算全部可见范围。
export async function searchV2(question: string): Promise<SearchV2Result> {
  const res = await http.post('/api/search/v2', { question, top_k: 5 })
  return res.data
}

