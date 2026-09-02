import http from './http'

export interface WikiPageSummary {
  id: string
  title: string
  summary: string
  status: string
  locked: boolean
  category: string
  latest_version: string | null
  current_revision_id: string | null
  preview_revision_id: string | null
  has_preview: boolean
  updated_at: string | null
}

// J-2：Wiki Citation 不再携带 Card 分支（旧 Card 驱动 Wiki 遗留）。
export interface WikiCitation {
  id: string
  evidence?: {
    id: string
    evidence_type: string
    preview: string
    page_id: string | null
    page_title: string | null
    source_url: string | null
  } | null
}

export interface WikiSection {
  id: string
  section_type: string
  heading: string | null
  content: string
  locked: boolean
  citations: WikiCitation[]
  version_label: string | null
  is_common: boolean
  content_origin: string | null
  merge_policy: string | null
  version_status: string | null
  diff_notice: string | null
}

export interface WikiRelatedTopic {
  id: string
  title: string
  summary: string
  category: string
  relation_reason: string[]
}

export interface WikiDetail extends WikiPageSummary {
  sections: WikiSection[]
  related_topics: WikiRelatedTopic[]
  related_topic_ids: string[]
  viewing_revision_id?: string | null
}

export interface WikiRevision {
  id: string
  wiki_page_id: string
  parent_revision_id: string | null
  title: string
  summary: string
  status: string
  created_at: string | null
}

export interface WikiDiff {
  added: Array<{ section_type: string; heading: string; new_content: string }>
  removed: Array<{ section_type: string; heading: string; old_content: string }>
  changed: Array<{ section_type: string; heading: string; old_content: string; new_content: string }>
}

export interface WikiRebuildStatus {
  running: boolean
  processed: number
  total: number
  created: number
  updated: number
  skipped: number
  failed: number
  message: string
}

export interface RefreshStatus {
  pending_pages: number
  pending_wikis: number
  backlog: number
  queue_capacity: number
  queue_occupied: number
}

export const wikiApi = {
  async list(params: { status?: string; q?: string; category?: string } = {}): Promise<WikiPageSummary[]> {
    const res = await http.get('/api/wiki', { params })
    return res.data.pages
  },

  async get(pageId: string, opts?: { preview?: boolean; revisionId?: string }): Promise<WikiDetail> {
    const res = await http.get(`/api/wiki/${pageId}`, {
      params: {
        preview: opts?.preview ? true : undefined,
        revision_id: opts?.revisionId,
      },
    })
    return res.data
  },

  async revisions(pageId: string): Promise<WikiRevision[]> {
    const res = await http.get(`/api/wiki/${pageId}/revisions`)
    return res.data.revisions
  },

  async diff(pageId: string, revisionId: string): Promise<WikiDiff> {
    const res = await http.get(`/api/wiki/${pageId}/diff/${revisionId}`)
    return res.data
  },

  async rebuild(): Promise<{ running: boolean } & WikiRebuildStatus> {
    const res = await http.post('/api/wiki/rebuild')
    return res.data
  },

  async rebuildStatus(): Promise<{ running: boolean } & WikiRebuildStatus> {
    const res = await http.get('/api/wiki/rebuild-status')
    return res.data
  },

  async refreshDirty(): Promise<{
    message: string
    dirty_pages_submitted: number
    dirty_pages_rejected: number
    dirty_wikis_submitted: number
    dirty_wikis_rejected: number
    pending: RefreshStatus
  }> {
    const res = await http.post('/api/wiki/refresh-dirty')
    return res.data
  },

  async refreshStatus(): Promise<RefreshStatus> {
    const res = await http.get('/api/wiki/refresh-status')
    return res.data
  },

  async preview(pageId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/wiki/${pageId}/preview`)
    return res.data
  },

  async archive(pageId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/wiki/${pageId}/archive`)
    return res.data
  },

  async publish(pageId: string, revisionId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/wiki/${pageId}/publish`, { revision_id: revisionId })
    return res.data
  },

  async rollback(pageId: string, revisionId: string): Promise<{ message: string }> {
    const res = await http.post(`/api/wiki/${pageId}/rollback/${revisionId}`)
    return res.data
  },

  async updateSection(
    pageId: string,
    revisionId: string,
    sectionId: string,
    content: string,
  ): Promise<{ message: string; locked: boolean }> {
    const res = await http.patch(`/api/wiki/${pageId}/revisions/${revisionId}/sections/${sectionId}`, { content })
    return res.data
  },

  async lockSection(pageId: string, revisionId: string, sectionId: string): Promise<{ message: string; locked: boolean }> {
    const res = await http.post(`/api/wiki/${pageId}/revisions/${revisionId}/sections/${sectionId}/lock`)
    return res.data
  },

  async unlockSection(pageId: string, revisionId: string, sectionId: string): Promise<{ message: string; locked: boolean }> {
    const res = await http.post(`/api/wiki/${pageId}/revisions/${revisionId}/sections/${sectionId}/unlock`)
    return res.data
  },
}
