import http from './http'

export interface Debt {
  id: string
  debt_type: string
  description: string
  related_question: string
  score: number
  status: string
  title: string
  root_cause: string | null
  scope_json: string | null
  priority: string | null
  occurrence_count: number
  resolution_card_id: string | null
  resolution_evidence_id: string | null
  first_seen_at: string | null
  last_seen_at: string | null
}

export const debtApi = {
  async list(params: { status?: string; debt_type?: string; limit?: number } = {}): Promise<Debt[]> {
    const res = await http.get('/api/knowledge/debts', { params })
    return res.data.debts || []
  },

  async createFromQuery(payload: {
    question: string
    root_cause: string
    intent?: string
    scope?: Record<string, unknown>
  }): Promise<{ candidate_id: string; debt_id: string | null; promoted: boolean }> {
    const res = await http.post('/api/knowledge/debts/from-query', payload)
    return res.data
  },

  async myRequests(): Promise<Debt[]> {
    const res = await http.get('/api/knowledge/debts/my-requests')
    return res.data.debts
  },

  async provideSource(debtId: string, evidenceId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/knowledge/debts/${debtId}/provide-source`, { evidence_id: evidenceId })
    return res.data
  },

  async linkCard(debtId: string, cardId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/knowledge/debts/${debtId}/link-card`, { card_id: cardId })
    return res.data
  },

  async revalidate(debtId: string): Promise<{ message: string; resolved: boolean }> {
    const res = await http.post(`/api/knowledge/debts/${debtId}/revalidate`)
    return res.data
  },

  async scan(): Promise<{ message: string }> {
    const res = await http.post('/api/knowledge/debts/scan')
    return res.data
  },

  async resolve(debtId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/knowledge/debts/${debtId}/resolve`)
    return res.data
  },
}
