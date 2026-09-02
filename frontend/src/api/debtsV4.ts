import http from './http'

export interface DebtV4 {
  id: string
  title: string
  status: string
}

export interface AccessRequestV4 {
  id: string
  original_query: string
  groups: string[]
  status: string
  target_notebook_id: string | null
}

export const debtsV4Api = {
  async list(params: { status?: string; limit?: number } = {}): Promise<DebtV4[]> {
    const { data } = await http.get('/api/v4/debts', { params })
    return data.debts || []
  },

  async listAccessRequests(params: { status?: string } = {}): Promise<AccessRequestV4[]> {
    const { data } = await http.get('/api/v4/access-requests', { params })
    return data.requests || []
  },

  async revalidateAccessRequest(id: string): Promise<{ resolved: boolean }> {
    const { data } = await http.post(`/api/v4/access-requests/${id}/revalidate`)
    return data
  },
}
