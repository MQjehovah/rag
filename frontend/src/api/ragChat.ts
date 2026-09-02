import http from './http'

export interface WikiResultView {
  wiki_page_id: string
  title: string
  content: string
  summary: string
  score: number
  updated_at: string | null
  manually_edited: boolean
  locked: boolean
  version_label: string | null
  is_common: boolean
  latest_version: string | null
  diff_notice: string | null
  link: string
}

export interface RawResultView {
  chunk_id: string
  page_id: string
  title: string
  source_type: string | null
  chunk_index: number
  content: string
  updated_at: string | null
  link: string
}

export type ServiceStatus = 'used' | 'degraded' | 'not_used'

export interface RagChatResponse {
  retrieval_completed: boolean
  answer_eligible: boolean
  service_degraded: boolean
  response_mode: 'answer' | 'retrieval_only' | 'insufficient'
  answer_source_mode: 'wiki' | 'raw' | 'none'
  model_status: ServiceStatus
  embedding_status: ServiceStatus
  reranker_status: ServiceStatus
  degraded_reasons: string[]
  answer: string | null
  wiki_results: WikiResultView[]
  raw_results: RawResultView[]
  knowledge_missing: boolean
  answer_id: string
}

export interface ScopeOption {
  value: string
  label: string
}

export const ragChatApi = {
  // J-1：Chat 不再要求前端提交权限域，后端自动计算全部可见范围。
  async ask(query: string): Promise<RagChatResponse> {
    const { data } = await http.post<RagChatResponse>('/api/chat', { query })
    return data
  },

  async feedback(payload: {
    answer_id: string
    helpful: boolean
    reason?: 'incorrect' | 'incomplete' | null
    note?: string | null
  }): Promise<void> {
    await http.post('/api/v4/answer-feedback', payload)
  },

  async answerNeeded(answer_id: string): Promise<void> {
    await http.post('/api/v4/answer-needed', { answer_id })
  },
}

