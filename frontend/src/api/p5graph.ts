import http from './http'

export interface P5Entity {
  id: string
  entity_type: string
  name: string
  normalized: string
  disambiguation_status: string  // confirmed / pending / manual_review
  confidence: number
  aliases: string[]
  card_ids: string[]
  cards: P5CardNode[]
}

export interface P5Relation {
  id: string
  source_entity_id: string
  target_entity_id: string
  relation_type: string
  card_id: string      // 来源 Card
  claim_id: string | null
  evidence_id: string | null
  confidence: number
  status: string
}

export interface P5CardNode {
  id: string
  canonical_title: string
  card_type: string
}

export interface P5CardEntityLink {
  card_id: string
  entity_ids: string[]
}

export interface P5GraphStats {
  entity_count: number
  relation_count: number
  published_card_count: number
  linked_card_count: number
  unlinked_card_count: number
}

export interface P5Community {
  id: string
  title: string
  summary: string
  entity_count: number
  card_count: number
  community_key?: string | null
  source_hash?: string | null
  algorithm?: string | null
  dirty?: boolean
  status?: string
  member_entity_ids: string[]
  cited_card_ids: string[]
  cited_cards: P5CardNode[]
}

export interface P5GraphData {
  entities: P5Entity[]
  relations: P5Relation[]
  cards: P5CardNode[]
  card_entity_links: P5CardEntityLink[]
  stats: P5GraphStats
  truncated: boolean
}

export interface P5CardEntity {
  id: string
  name: string
  entity_type: string
}

export interface P5CardRelation {
  id: string
  source_entity_id: string
  source_entity_name: string | null
  source_entity_type: string | null
  target_entity_id: string
  target_entity_name: string | null
  target_entity_type: string | null
  relation_type: string
  confidence: number
}

export interface P5CardGraphDetail {
  card: P5CardNode
  entities: P5CardEntity[]
  relations: P5CardRelation[]
  communities: Array<{ id: string; title: string }>
}

export const p5GraphApi = {
  async entities(entityType?: string): Promise<P5Entity[]> {
    const res = await http.get('/api/p5/graph/entities', { params: { entity_type: entityType } })
    return res.data.entities
  },

  async entity(entityId: string): Promise<P5Entity> {
    const res = await http.get(`/api/p5/graph/entities/${entityId}`)
    return res.data
  },

  async relations(cardId?: string): Promise<P5Relation[]> {
    const res = await http.get('/api/p5/graph/relations', { params: { card_id: cardId } })
    return res.data.relations
  },

  async graph(): Promise<P5GraphData> {
    const res = await http.get('/api/p5/graph/graph')
    return res.data
  },

  async unlinkedCards(): Promise<P5CardNode[]> {
    const res = await http.get('/api/p5/graph/unlinked-cards')
    return res.data.cards
  },

  async cardGraph(cardId: string): Promise<P5CardGraphDetail> {
    const res = await http.get(`/api/p5/graph/cards/${cardId}`)
    return res.data
  },

  async communities(): Promise<P5Community[]> {
    const res = await http.get('/api/p5/graph/communities')
    return res.data.communities
  },

  async rebuild(): Promise<{ message: string }> {
    const res = await http.post('/api/p5/graph/rebuild')
    return res.data
  },
}
