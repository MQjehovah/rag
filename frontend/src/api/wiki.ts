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
  workspace_id?: string | null
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
  // Phase 8B（见 backend DISPLAY_CONTRACT.md）：只读接口增量字段。
  // section_role 由存储 structure_json 的合法 role 解析；display 合法时为受限 DTO，
  // 否则为 null（前端回退 Markdown）。普通/历史/人工章节两者皆可缺省/null。
  section_role?: string | null
  display?: ApiSectionDisplay | null
}

// ===== Phase 8B display DTO（与 DISPLAY_CONTRACT.md 白名单一致，未知键前端不消费） =====

export interface ApiSectionDisplay {
  schema_version: string
  content_hash: string
  section_role: string
  version_scope: string
  endpoint: ApiEndpointMeta
  parameters: ApiEndpointParam[]
  request_body: ApiRequestBody | null
  responses: ApiResponseRow[]
  error_codes: ApiErrorCodeRow[]
  examples: ApiExampleRow[]
  version_notes: ApiVersionNoteRow[]
  knowledge_gaps: ApiGapRow[]
  conflicts: ApiConflictRow[]
}

export interface ApiEndpointMeta {
  method: string
  path: string
  summary: string
  description: string
}

export interface ApiEndpointParam {
  location: 'path' | 'query' | 'header'
  name: string
  required: boolean
  description: string
  /** schema.type 的显式声明（字符串或多类型按 “a | b”）；空串 = 未提供（不推测）。 */
  type: string
}

export interface ApiRequestBody {
  required: boolean
  description: string
  media_types: ApiMediaType[]
}

/** 逐媒体类型保留名称；schema_status 保守语义（present/unspecified），无布尔断言。 */
export interface ApiMediaType {
  media_type: string
  schema_status: 'present' | 'unspecified'
}

export interface ApiResponseRow {
  status_code: string
  description: string
  media_types: ApiMediaType[]
}

export interface ApiErrorCodeRow {
  code: string
  description: string
  /** 空串 = 未提供（业务码≠HTTP 状态）；禁止并入 HTTP 状态列。 */
  http_status: string
}

export interface ApiExampleRow {
  title: string
  description: string
  media_type: string
  /** JSON 安全对象/数组/标量；前端仅经 JSON.stringify + <pre> 文本展示。 */
  content: unknown
}

export interface ApiVersionNoteRow {
  version_scope: string
  note: string
}

export interface ApiGapRow {
  gap_type: string
  description: string
}

export interface ApiConflictRow {
  field_path: string
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

// ===== Phase 8C：章节 Evidence 追溯（只读，见 docs/phase-8c-contract.md §1） =====

/** 受限 locator 白名单：page_number/heading/image_id/content_type，绝不消费 chunk_id/bbox 等。 */
export interface WikiSectionEvidenceLocator {
  page_number?: number | null
  heading?: string | null
  image_id?: string | null
  content_type?: string | null
}

export interface EvidenceBindingUse {
  field_path: string
  usage_type: 'support' | 'conflict' | string
}

export interface WikiSectionEvidenceItem {
  evidence_id: string
  evidence_type: string
  /** EvidenceItem.status：active/stale/rejected */
  status: string
  /** 绑定快照 evidence_content_hash == EvidenceItem.content_hash */
  hash_matches: boolean
  /** active_current / changed / stale / rejected */
  state: string
  /** 已授权原文（服务端截断至 2000 字符） */
  content: string
  content_truncated: boolean
  locator: WikiSectionEvidenceLocator
  source_display_name: string
  /** 同 Evidence 多条 Binding 按 evidence_id 去重后聚合 */
  bindings: EvidenceBindingUse[]
}

export interface WikiSectionEvidenceResult {
  wiki_id: string
  revision_id: string
  section_id: string
  total: number
  limit: number
  offset: number
  items: WikiSectionEvidenceItem[]
}

// ===== Phase 8C：编辑者只读诊断（见 docs/phase-8c-contract.md §2） =====

export interface WikiDiagnosticsSkill {
  key: string
  display_name: string
  version: string | null
  /** auto/manual/none */
  selection: string
  selected_by: string | null
  locked: boolean
  /** 仅受控 reason_code；未知/内部 → "unknown" */
  reason_code: string | null
}

export interface WikiDiagnosticsSectionStatus {
  heading: string
  /** NULL → unknown（历史全 NULL 不得算作通过） */
  validation_status: string | null
}

export interface WikiDiagnosticsValidation {
  summary: string
  sections: WikiDiagnosticsSectionStatus[]
}

export interface WikiDiagnostics {
  wiki_id: string
  editable: boolean
  is_current_wiki_config: boolean
  skill: WikiDiagnosticsSkill | null
  validation: WikiDiagnosticsValidation | null
}

export const wikiApi = {
  /** workspaceId 必传：目录请求必须限定在当前工作区（Phase 8A）。 */
  async list(
    params: { workspaceId?: string; status?: string; q?: string; category?: string } = {},
  ): Promise<WikiPageSummary[]> {
    const res = await http.get('/api/wiki', {
      params: {
        workspace_id: params.workspaceId,
        status: params.status,
        q: params.q,
        category: params.category,
      },
    })
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

  /** Phase 8C：按 Section 查询其绑定的可授权 Evidence（只读）。 */
  async sectionEvidence(params: {
    wikiId: string
    revisionId: string
    sectionId: string
    limit?: number
    offset?: number
  }): Promise<WikiSectionEvidenceResult> {
    const res = await http.get(
      `/api/wiki/${params.wikiId}/revisions/${params.revisionId}/sections/${params.sectionId}/evidence`,
      { params: { limit: params.limit ?? 50, offset: params.offset ?? 0 } },
    )
    return res.data
  },

  /** Phase 8C：编辑者只读诊断（Skill 配置 + 当前查看 Revision 的章节校验摘要）。 */
  async wikiDiagnostics(wikiId: string): Promise<WikiDiagnostics> {
    const res = await http.get(`/api/wiki/${wikiId}/diagnostics`)
    return res.data
  },
}
