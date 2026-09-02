import http from './http'

export type CardType = 'guide' | 'reference' | 'decision'

export type CardStatus = 'draft' | 'pending' | 'published' | 'rejected' | 'archived'

export type ChangeType = 'NEW' | 'ENRICH' | 'UPDATE' | 'CONFLICT' | 'SUPERSEDE' | 'NO_CHANGE' | 'POSSIBLE_DUPLICATE'

export interface CardScope {
  product?: string | null
  version?: string | null
  region?: string | null
}

export interface CardBlock {
  block_type: string
  heading: string | null
  content: string
  order_index: number
}

export interface CardClaim {
  claim_type: string
  statement: string
  confidence: number
  status?: string
  evidence_count?: number
}

export interface KnowledgeCard {
  id: string
  card_type: CardType
  canonical_title: string
  summary: string
  scope: CardScope
  status: CardStatus
  risk_level: string | null
  confidence: number
  current_revision_id: string | null
  draft_revision_id: string | null
  viewing_revision_id?: string | null
  content_hash: string | null
  created_at: string | null
  updated_at: string | null
}

export interface CardDetail extends KnowledgeCard {
  blocks: CardBlock[]
  claims: CardClaim[]
  graph_status?: 'unlinked' | 'entities_only' | 'linked'
  entity_count?: number
  relation_count?: number
  community_count?: number
  viewing_revision_kind?: 'published' | 'review'
  revision_body?: string
  has_draft_revision?: boolean
}

export interface CardRevision {
  id: string
  card_id: string
  parent_revision_id: string | null
  structured: { blocks?: CardBlock[]; claims?: any[] }
  change_type: ChangeType
  change_summary: string
  generation_method: string | null
  model_name: string | null
  status: string
  created_at: string | null
}

export interface CardDiff {
  diff: {
    added_blocks: number
    removed_blocks: number
    changed_blocks: number
    added_claims: number
    removed_claims: number
  }
  changes: Record<string, any[]>
}

export interface CardEvidence {
  id: string
  evidence_type: string
  content: string
  status: string
  needs_review?: boolean
  inferred?: boolean
  observations?: Array<{
    id: string
    observation_type: string
    content: string
    inferred?: boolean
    needs_review?: boolean
    analysis_status?: string
  }>
}

export interface CardSource {
  id: string
  page_id: string | null
  evidence_id: string | null
  contribution_type: string | null
  source_version?: string | null
  page_title?: string | null
  source_type?: string | null
  source_url?: string | null
  evidence_type?: string | null
  evidence_content?: string | null
  evidence_needs_review?: boolean | null
  page_number?: string | number | null
  heading?: string | null
  section_path?: string | null
}

export interface ReviewQueueCard extends KnowledgeCard {
  change_type: ChangeType
  change_summary: string
  match_path: string
  similar_card_id: string | null
  compile_run_id: string
  source_page_id: string
  page_title: string
  source_version?: string | null
  evidence_complete: boolean
  image_status: string
  requires_individual_review: boolean
}

export interface ReviewQueueGroup {
  compile_run_id: string
  source_page_id: string
  page_title: string
  cards: ReviewQueueCard[]
  low_risk_ids: string[]
}

export const cardApi = {
  async list(params: { card_type?: string; status?: string; limit?: number } = {}): Promise<KnowledgeCard[]> {
    const res = await http.get('/api/cards', { params })
    return res.data.cards
  },

  async get(id: string, view: 'published' | 'review' = 'published'): Promise<CardDetail> {
    const res = await http.get(`/api/cards/${id}`, { params: { view } })
    return res.data
  },

  async revisions(id: string): Promise<CardRevision[]> {
    const res = await http.get(`/api/cards/${id}/revisions`)
    return res.data.revisions
  },

  async diff(id: string, revisionId: string): Promise<CardDiff> {
    const res = await http.get(`/api/cards/${id}/diff/${revisionId}`)
    return res.data
  },

  async approveAndPublish(id: string): Promise<{
    message: string
    card_id: string
    revision_id: string
    status: 'published'
    already_published: boolean
    superseded: string[]
  }> {
    const res = await http.post(`/api/cards/${id}/approve`)
    return res.data
  },

  async reject(id: string): Promise<{ message: string }> {
    const res = await http.post(`/api/cards/${id}/reject`)
    return res.data
  },

  async archive(id: string): Promise<{ message: string }> {
    const res = await http.post(`/api/cards/${id}/archive`)
    return res.data
  },

  async reviewQueue(): Promise<{ groups: ReviewQueueGroup[]; total_cards: number }> {
    const res = await http.get('/api/cards/review-queue')
    return res.data
  },

  async batchPublish(cardIds: string[]): Promise<{
    message: string
    published: string[]
    skipped: { card_id: string; reason: string }[]
    reviewed_by: string | null
    reviewed_at: string
  }> {
    const res = await http.post('/api/cards/batch-publish', { card_ids: cardIds })
    return res.data
  },

  async evidence(id: string): Promise<CardEvidence[]> {
    const res = await http.get(`/api/cards/${id}/evidence`)
    return res.data.evidence
  },

  async sources(id: string): Promise<CardSource[]> {
    const res = await http.get(`/api/cards/${id}/sources`)
    return res.data.sources
  },
}
