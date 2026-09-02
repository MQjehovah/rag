import http from './http'

// V4 实体关系图谱（J-3）。

export interface V4GraphNode {
  id: string
  display_name: string
  entity_type: string
  version_status: string
  community: { key: string; display_name: string } | null
  related_wiki: Array<{ id: string; title: string }>
}

export interface V4Facets {
  versions: string[]
  entity_types: string[]
  communities: V4Community[]
}

export interface V4GraphEdge {
  source: string
  target: string
  relation_label: string
  version_label: string | null
  version_status: string
  evidence_count: number
  conflict: boolean
}

export interface V4Community {
  key: string
  display_name: string
  color_index: string
}

export interface V4Subgraph {
  nodes: V4GraphNode[]
  edges: V4GraphEdge[]
  communities: V4Community[]
  total_nodes: number
  total_edges: number
  truncated: boolean
}

export interface SubgraphParams {
  q?: string
  entity_type?: string
  version?: string
  community?: string
  depth?: number
  limit?: number
  focus?: string
}

export const graphApi = {
  async subgraph(params: SubgraphParams = {}): Promise<V4Subgraph> {
    const { data } = await http.get('/api/v4/graph/subgraph', { params })
    return data
  },

  async communities(): Promise<V4Community[]> {
    const { data } = await http.get('/api/v4/graph/communities')
    return data.communities
  },

  async search(q: string, entityType?: string): Promise<Array<{ id: string; display_name: string; entity_type: string }>> {
    const { data } = await http.get('/api/v4/graph/search', { params: { q, entity_type: entityType } })
    return data.entities
  },

  async facets(): Promise<V4Facets> {
    const { data } = await http.get('/api/v4/graph/facets')
    return data
  },
}
