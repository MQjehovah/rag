import http from './http'

export interface EvidenceLocator {
  page_number?: number
  chunk_index?: number
  content_type?: string
  heading?: string
  image_id?: string
  image_url?: string
  chunk_id?: string
}

export interface EvidenceSource {
  page_id: string | null
  title: string
  chunk_id: string | null
}

export interface EvidenceItem {
  id: string
  evidence_type: string
  content: string
  locator: EvidenceLocator
  content_hash: string | null
  source_doc_hash: string | null
  extraction_method: string | null
  model_name: string | null
  confidence: number | null
  needs_review: boolean
  status: string
  source: EvidenceSource
}

export interface Observation {
  id: string
  asset_id: string
  observation_type: string
  content: string
  extraction_method: string | null
  model_name: string | null
  confidence: number | null
  needs_review: boolean
  inferred?: boolean
  analysis_status?: string
  provider?: string | null
  error_category?: string | null
  created_at: string | null
  locator: EvidenceLocator
}

export interface EvidenceDetail extends EvidenceItem {
  observations: Observation[]
}

export const evidenceApi = {
  async byPage(pageId: string): Promise<EvidenceItem[]> {
    const res = await http.get('/api/evidence', { params: { page_id: pageId } })
    return res.data.evidence
  },

  async byAsset(assetId: string): Promise<Observation[]> {
    const res = await http.get('/api/evidence', { params: { asset_id: assetId } })
    return res.data.observations
  },

  async detail(evidenceId: string): Promise<EvidenceDetail> {
    const res = await http.get(`/api/evidence/${evidenceId}`)
    return res.data
  },

  async upsertManualObservation(assetId: string, content: string): Promise<Observation> {
    const res = await http.post('/api/evidence/observations', { asset_id: assetId, content })
    return res.data
  },
}
