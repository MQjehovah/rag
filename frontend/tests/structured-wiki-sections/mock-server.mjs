// Phase 8B 结构化 Wiki 章节展示：浏览器验收用隔离请求 mock（内存，零 DB/模型）。
//
// 监听 MOCK_PORT（默认 8001，与 acceptance 代理目标一致），模拟：
//   - 认证：GET /api/auth/me（Bearer tok-edit）
//   - 工作区：GET /api/wiki-workspaces、GET /api/wiki-workspaces/{id}
//   - Wiki：GET /api/wiki（目录）、GET /api/wiki/{pageId}（详情，含 section_role/display）、
//            GET /api/wiki/{pageId}/revisions、GET /api/wiki/{pageId}/diff/{rev}、
//            PATCH /api/wiki/{pageId}/revisions/{rev}/sections/{sectionId}（人工编辑后结构消失）
//   - Evidence：GET /api/evidence?page_id=、GET /api/evidence/{id}
// 由 accept.mjs 作为独立子进程启动；与 DISPLAY_CONTRACT.md 的 DTO 逐字段对齐。
import http from 'node:http'
import { createHash } from 'node:crypto'
import { URL } from 'node:url'

const PORT = Number(process.env.MOCK_PORT || 8001)

function hashOf(content) {
  return createHash('sha256').update(String(content || ''), 'utf8').digest('hex')
}

const users = {
  'tok-edit': {
    id: 'u-edit', username: 'edit', display_name: '编辑用户', email: 'edit@test',
    is_local: true, groups: ['engineering'], is_admin: false, is_wiki_editor: true,
  },
}

const now = '2026-09-04T09:00:00.000000'
const workspaces = [
  { id: 'ws-eng', key: 'ws_key_eng', name: '工程工作区', description: '接口与工程主题', acl_scope: '{"groups":["engineering"]}', scope_id: 'group:engineering', status: 'active', created_by: 'u-admin', created_at: now, updated_at: now },
]

function makeCitation(id, evidenceId, preview) {
  return { id, evidence: { id: evidenceId, evidence_type: 'text', preview, page_id: 'p-plain-src', page_title: '安装指导(原始)', source_url: null } }
}

function makeSection({ id, section_type, heading, content, display, locked = false, version_label = null, is_common = false, content_origin = 'compiled', merge_policy = 'auto', version_status = 'current', diff_notice = null, section_role = null }) {
  const s = {
    id, section_type, heading, content, locked, citations: [], version_label, is_common,
    content_origin, merge_policy, version_status, diff_notice,
    section_role, display: display || null,
  }
  return s
}

// —— 契约展示 DTO 构造（字段名与 DISPLAY_CONTRACT.md 白名单一致） ——
function makeDisplay({ version_scope, method, path, summary, description, parameters = [], request_body = null, responses = [], error_codes = [], examples = [], version_notes = [], knowledge_gaps = [], conflicts = [] }) {
  return {
    schema_version: 'api-section-display/v1',
    content_hash: null, // 由该 section content 计算后回填
    section_role: 'endpoint',
    version_scope,
    endpoint: { method, path, summary, description },
    parameters,
    request_body,
    responses,
    error_codes,
    examples,
    version_notes,
    knowledge_gaps,
    conflicts,
  }
}

// ===================== pageA：用户接口手册 =====================
const pUsers = {
  id: 'p-users',
  workspace_id: 'ws-eng',
  title: '用户接口手册',
  summary: '覆盖用户管理接口的调用约定与字段说明。',
  status: 'published',
  category: '用户接口',
  keywords: ['用户', 'users', '接口'],
  locked: false,
  latest_version: 'v2.0',
  current_revision_id: 'r1',
  preview_revision_id: null,
  has_preview: false,
  updated_at: '2026-09-03T10:00:00',
}

// 全局 knowledge_gaps / version_notes（角色非 endpoint，走 Markdown）
const gapsGlobalUsers = makeSection({
  id: 's-users-gaps', section_type: 'gaps', heading: '知识缺口（全局）',
  content: '# 知识缺口\n\n- 用户搜索接口的排序规则尚未验证。\n- 批量删除接口暂未纳入本文档。\n',
})
const notesGlobalUsers = makeSection({
  id: 's-users-notes', section_type: 'version_notes', heading: '版本说明（全局）',
  content: '# 版本说明\n\n- v1：2026-03 初始版本。\n- v2：2026-08 用户详情路径由 `/users?name=` 调整为 `/users/{id}`。\n',
})

// GET /users [v1]：结构化展示核心样例
const sUsersGetContent = '## GET /users（v1）\n\n分页获取用户列表。\n\n### 参数\n- query `page`：页码\n- query `page_size`：每页条数\n'
const sUsersGetDisplay = makeDisplay({
  version_scope: 'v1', method: 'GET', path: '/users',
  summary: '获取用户列表',
  description: '分页返回用户列表，可按名称过滤。',
  parameters: [
    { location: 'query', name: 'page', required: false, description: '页码，从 1 开始。' },
    { location: 'query', name: 'page_size', required: false, description: '每页条数。' },
  ],
  request_body: null,
  responses: [
    { status_code: '200', description: '成功返回用户列表', schema_present: true },
    { status_code: '401', description: '未认证', schema_present: false },
  ],
  error_codes: [
    { code: 'TOKEN_EXPIRED', description: '访问令牌已过期', http_status: '401' },
    { code: 'RATE_LIMITED', description: '请求过于频繁', http_status: '' },
  ],
  examples: [
    { title: '分页查询示例', description: '', media_type: 'application/json', content: { page: 1, page_size: 20, items: [{ id: 1, name: '张三' }] } },
  ],
  version_notes: [{ version_scope: 'v1', note: 'v1 起采用分页返回用户列表。' }],
  knowledge_gaps: [{ gap_type: 'missing_responses', description: '部分 4xx 响应体结构未知。' }],
  conflicts: [{ field_path: 'responses.200' }],
})
const sUsersGet = makeSection({ id: 's-users-get', section_type: 'endpoint', heading: 'GET /users（v1）', version_label: 'v1', content: sUsersGetContent, section_role: 'endpoint' })
sUsersGet.display = { ...sUsersGetDisplay, content_hash: hashOf(sUsersGetContent) }

// POST /users [v1]：供“人工编辑保存后结构消失”场景（编辑该节）
const sUsersPostContent = '## POST /users（v1）\n\n创建单个用户。\n\n### 请求体\n- `application/json` 用户信息\n'
const sUsersPostDisplay = makeDisplay({
  version_scope: 'v1', method: 'POST', path: '/users',
  summary: '创建用户',
  description: '',
  parameters: [
    { location: 'header', name: 'Content-Type', required: true, description: '请求媒体类型，固定 application/json。' },
  ],
  request_body: {
    required: true,
    description: '新用户信息',
    media_types: [{ media_type: 'application/json', schema_present: true }],
  },
  responses: [
    { status_code: '200', description: '创建成功', schema_present: true },
    { status_code: '400', description: '请求体校验失败', schema_present: false },
  ],
  error_codes: [
    { code: 'INVALID_BODY', description: '请求体校验未通过', http_status: '400' },
    { code: 'USERNAME_TAKEN', description: '用户名已存在', http_status: '' },
  ],
  examples: [
    { title: '创建用户请求', description: '', media_type: 'application/json', content: { name: '李四', email: 'li@example.com' } },
  ],
  version_notes: [{ version_scope: 'v1', note: 'v1 要求 Content-Type 请求头。' }],
  knowledge_gaps: [{ gap_type: 'missing_error_responses', description: '用户名规范未验证。' }],
  conflicts: [],
})
const sUsersPost = makeSection({ id: 's-users-post', section_type: 'endpoint', heading: 'POST /users（v1）', version_label: 'v1', content: sUsersPostContent, section_role: 'endpoint' })
sUsersPost.display = { ...sUsersPostDisplay, content_hash: hashOf(sUsersPostContent) }

// GET /users/{id} [v2]：路径参数 + XSS 占位描述（安全断言载体）
const sUsersGetIdContent = '## GET /users/{id}（v2）\n\n按路径参数 userId 获取用户详情。\n'
const sUsersGetIdDisplay = makeDisplay({
  version_scope: 'v2', method: 'GET', path: '/users/{id}',
  summary: '获取单个用户详情',
  description: '返回指定用户的详情。<img id="xss-leak" src=x onerror="window.__xss=1"> 该占位文本应始终以纯文本展示，绝不作为 HTML 注入。',
  parameters: [
    { location: 'path', name: 'userId', required: true, description: '用户唯一标识。' },
  ],
  request_body: null,
  responses: [
    { status_code: '200', description: '成功返回用户详情', schema_present: true },
    { status_code: '404', description: '用户不存在', schema_present: false },
  ],
  error_codes: [
    { code: 'USER_NOT_FOUND', description: '用户不存在', http_status: '404' },
    { code: 'RATE_LIMITED', description: '请求过于频繁', http_status: '' },
  ],
  examples: [
    { title: '查询用户详情', description: '', media_type: 'application/json', content: { id: 42, name: '王五', email: 'wang@example.com' } },
  ],
  version_notes: [{ version_scope: 'v2', note: 'v2 起用户标识改由路径参数 userId 传入。' }],
  knowledge_gaps: [],
  conflicts: [],
})
const sUsersGetId = makeSection({ id: 's-users-getid', section_type: 'endpoint', heading: 'GET /users/{id}（v2）', version_label: 'v2', content: sUsersGetIdContent, section_role: 'endpoint' })
sUsersGetId.display = { ...sUsersGetIdDisplay, content_hash: hashOf(sUsersGetIdContent) }

// 历史遗留 endpoint：role=null（display 未生成），应回退 Markdown 原文
const sUsersLegacy = makeSection({
  id: 's-users-legacy', section_type: 'endpoint', heading: 'GET /legacy_users（遗留说明）', version_label: 'unversioned',
  content: '## GET /legacy_users（历史遗留）\n\n该接口已由 GET /users 取代。本文以 Markdown 呈现，无结构化表格。\n\n| 字段 | 说明 |\n| --- | --- |\n| id | 旧版用户 ID |\n',
})

// 人工/保护初始章节：display=null，Markdown
const sUsersManual = makeSection({
  id: 's-users-manual', section_type: 'summary', heading: '人工维护说明', locked: true, content_origin: 'manual', merge_policy: 'protected', version_status: 'manual',
  content: '本接口文档的安全要求由管理员人工维护，重建不会自动覆盖。',
})

const sUsersOverview = makeSection({
  id: 's-users-ov', section_type: 'overview', heading: '主题概览',
  content: '# 用户接口手册\n\n本手册覆盖用户管理相关接口。所有请求须携带 `Authorization` 请求头。\n',
})

pUsers.sections = [
  sUsersOverview,
  sUsersGet,
  sUsersPost,
  sUsersGetId,
  sUsersLegacy,
  sUsersManual,
  gapsGlobalUsers,
  notesGlobalUsers,
]

// ===================== pageB：订单接口手册 =====================
const pOrders = {
  id: 'p-orders',
  workspace_id: 'ws-eng',
  title: '订单接口手册',
  summary: '覆盖订单查询接口的约定。',
  status: 'published',
  category: '订单接口',
  keywords: ['订单', 'orders'],
  locked: false,
  latest_version: 'v1.0',
  current_revision_id: 'r1',
  preview_revision_id: null,
  has_preview: false,
  updated_at: '2026-09-03T11:00:00',
}

const sOrdersGetContent = '## GET /orders（v1）\n\n分页获取订单列表。\n'
const sOrdersGetDisplay = makeDisplay({
  version_scope: 'v1', method: 'GET', path: '/orders',
  summary: '获取订单列表',
  description: '分页返回订单列表，可按状态过滤。',
  parameters: [
    { location: 'query', name: 'status', required: false, description: '订单状态过滤。' },
  ],
  request_body: null,
  responses: [
    { status_code: '200', description: '成功返回订单列表', schema_present: true },
    { status_code: '401', description: '未认证', schema_present: false },
  ],
  error_codes: [
    { code: 'ORDER_NOT_FOUND', description: '订单不存在', http_status: '404' },
  ],
  examples: [
    { title: '按状态查询', description: '', media_type: 'application/json', content: { status: 'paid', items: [{ id: 9, total: 120 }] } },
  ],
  version_notes: [{ version_scope: 'v1', note: 'v1 初始版本。' }],
  knowledge_gaps: [{ gap_type: 'missing_responses', description: '分页总数语义待补。' }],
  conflicts: [],
})
const sOrdersGet = makeSection({ id: 's-orders-get', section_type: 'endpoint', heading: 'GET /orders（v1）', version_label: 'v1', content: sOrdersGetContent, section_role: 'endpoint' })
sOrdersGet.display = { ...sOrdersGetDisplay, content_hash: hashOf(sOrdersGetContent) }

const sOrdersGaps = makeSection({
  id: 's-orders-gaps', section_type: 'gaps', heading: '知识缺口（全局）',
  content: '# 知识缺口\n\n- 订单金额精度规则未验证。\n',
})
const sOrdersNotes = makeSection({
  id: 's-orders-notes', section_type: 'version_notes', heading: '版本说明（全局）',
  content: '# 版本说明\n\n- v1：2026-04 初始版本。\n',
})
const sOrdersOverview = makeSection({
  id: 's-orders-ov', section_type: 'overview', heading: '主题概览',
  content: '# 订单接口手册\n\n本文档说明订单接口的调用方式与响应约定。\n',
})

pOrders.sections = [sOrdersOverview, sOrdersGet, sOrdersGaps, sOrdersNotes]

// ===================== 普通 wiki（回归：编辑/Revision/Diff/Evidence） =====================
const pPlain = {
  id: 'p-plain',
  workspace_id: 'ws-eng',
  title: '设备安装指南',
  summary: '普通 Wiki 页，验证既有交互回归。',
  status: 'published',
  category: '综合主题',
  keywords: ['安装', '设备'],
  locked: false,
  latest_version: 'v1.2',
  current_revision_id: 'r2',
  preview_revision_id: null,
  has_preview: false,
  updated_at: '2026-09-02T10:00:00',
}

const sPlainSummary = makeSection({
  id: 's-plain-sum', section_type: 'summary', heading: '主题概览',
  content: '该主题演示普通 Wiki 页面的历史修订与证据回溯能力。',
})
sPlainSummary.citations = [makeCitation('c-plain-1', 'ev-plain-1', '原始手册节选：必须使用扭矩扳手…')]

const sPlainSteps = makeSection({
  id: 's-plain-steps', section_type: 'steps', heading: '操作步骤',
  content: '1. 关闭电源。\n2. 使用扭矩扳手按 25 N·m 紧固螺栓。\n',
})
pPlain.sections = [sPlainSummary, sPlainSteps]

const wikiDb = { 'ws-eng': [pUsers, pOrders, pPlain] }

const revisionsDb = {
  'p-users': [
    { id: 'r1', wiki_page_id: 'p-users', parent_revision_id: null, title: '用户接口手册', summary: '含 v1/v2 接口说明', status: 'published', created_at: '2026-08-01T09:00:00' },
  ],
  'p-orders': [
    { id: 'r1', wiki_page_id: 'p-orders', parent_revision_id: null, title: '订单接口手册', summary: '初始版本', status: 'published', created_at: '2026-08-01T09:00:00' },
  ],
  'p-plain': [
    { id: 'r1', wiki_page_id: 'p-plain', parent_revision_id: null, title: '设备安装指南', summary: '初版', status: 'published', created_at: '2026-08-01T09:00:00' },
    { id: 'r2', wiki_page_id: 'p-plain', parent_revision_id: 'r1', title: '设备安装指南', summary: '修订步骤', status: 'published', created_at: '2026-08-20T09:00:00' },
  ],
}

const diffOf = {
  'p-users': { added: [], removed: [], changed: [{ section_type: 'endpoint', heading: 'GET /users（v1）', old_content: '旧', new_content: '新' }] },
  'p-plain': { added: [], removed: [], changed: [{ section_type: 'steps', heading: '操作步骤', old_content: '旧步骤', new_content: '新增扭矩扳手要求' }] },
}

const evidenceByPage = {
  'p-plain-src': [
    { id: 'ev-plain-1', evidence_type: 'text', content: '原始手册节选：必须使用扭矩扳手按 25 N·m 紧固，禁止使用活动扳手。', locator: { page_number: 5, heading: '安装' }, content_hash: null, source_doc_hash: null, extraction_method: 'text', model_name: null, confidence: 0.9, needs_review: false, status: 'active', source: { page_id: 'src-9', title: '安装指导(原始)', chunk_id: null } },
  ],
}

function serializeWorkspace(w, admin) {
  if (!admin) return { id: w.id, name: w.name, description: w.description, status: w.status, created_at: w.created_at, updated_at: w.updated_at }
  return { ...w }
}
function serializeSection(s) {
  const { section_role, display, ...rest } = s
  return { ...rest, section_role: section_role ?? null, display: display ?? null }
}
function serializePageList(p) {
  const { keywords, sections, ...rest } = p
  return { ...rest, preview_revision_id: null }
}
function visibleWorkspaces(user) {
  const admin = !!user.is_admin
  return workspaces.filter((w) => {
    if (admin) return true
    if (w.status !== 'active') return false
    const groups = w.scope_id.startsWith('group:') ? w.scope_id.slice(6).split(',') : []
    return groups.some((g) => user.groups.includes(g))
  })
}

function userOf(req) {
  const m = /Bearer\s+(\S+)/.exec(req.headers.authorization || '')
  return (m && users[m[1]]) || null
}

function json(res, code, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' })
  res.end(body)
}

async function handle(req, res) {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  const path = url.pathname
  const method = req.method

  const user = userOf(req)
  if (!user) return json(res, 401, { detail: '未认证' })

  if (path === '/api/auth/me') return json(res, 200, user)

  if (path === '/api/wiki-workspaces' && method === 'GET') {
    const admin = !!user.is_admin
    return json(res, 200, { workspaces: visibleWorkspaces(user).map((w) => serializeWorkspace(w, admin)) })
  }
  let m = path.match(/^\/api\/wiki-workspaces\/([^/]+)$/)
  if (m && method === 'GET') {
    const ws = visibleWorkspaces(user).find((w) => w.id === m[1])
    if (!ws) return json(res, 404, { detail: '工作区不存在' })
    return json(res, 200, serializeWorkspace(ws, !!user.is_admin))
  }

  if (path === '/api/wiki' && method === 'GET') {
    const wsId = url.searchParams.get('workspace_id') || ''
    const rows = wikiDb[wsId] || []
    const pages = rows.filter((p) => user.is_admin || p.status === 'published').map(serializePageList)
    return json(res, 200, { pages })
  }

  m = path.match(/^\/api\/wiki\/([^/]+)\/revisions$/)
  if (m && method === 'GET') {
    const revs = (revisionsDb[m[1]] || []).filter((r) => user.is_admin || r.status === 'published')
    return json(res, 200, { revisions: revs })
  }

  m = path.match(/^\/api\/wiki\/([^/]+)\/diff\/([^/]+)$/)
  if (m && method === 'GET') {
    const pid = m[1]
    if (!(revisionsDb[pid] || []).some((r) => r.id === m[2])) return json(res, 404, { detail: 'Revision 不存在' })
    return json(res, 200, diffOf[pid] || { added: [], removed: [], changed: [] })
  }

  m = path.match(/^\/api\/wiki\/([^/]+)\/revisions\/([^/]+)\/sections\/([^/]+)$/)
  if (m && method === 'PATCH') {
    const pid = m[1]
    const sectionId = m[3]
    let body = ''
    for await (const chunk of req) body += chunk
    let content = ''
    try {
      content = JSON.parse(body || '{}').content || ''
    } catch {}
    const page = Object.values(wikiDb).flat().find((p) => p.id === pid)
    const sec = page && page.sections.find((s) => s.id === sectionId)
    if (!sec) return json(res, 404, { detail: 'Section 不存在' })
    // 模拟真实后端人工编辑后结构消失：role=null、display=null、content 用提交正文，
    // content_origin=manual、locked=true（既有 PATCH 生命周期语义）。
    sec.content = content
    sec.content_origin = 'manual'
    sec.merge_policy = 'protected'
    sec.locked = true
    sec.section_role = null
    sec.display = null
    return json(res, 200, { message: 'Section 已保存并生效', locked: true })
  }

  m = path.match(/^\/api\/wiki\/([^/]+)$/)
  if (m && method === 'GET') {
    const page = Object.values(wikiDb).flat().find((p) => p.id === m[1])
    if (!page || (!user.is_admin && page.status !== 'published')) return json(res, 404, { detail: '主题页不存在' })
    return json(res, 200, {
      ...serializePageList(page),
      sections: (page.sections || []).map(serializeSection),
      related_topics: [],
      related_topic_ids: [],
      viewing_revision_id: 'r1',
    })
  }

  if (path === '/api/evidence' && method === 'GET') {
    const pid = url.searchParams.get('page_id')
    return json(res, 200, { evidence: pid ? (evidenceByPage[pid] || []) : [] })
  }

  m = path.match(/^\/api\/evidence\/([^/]+)$/)
  if (m && method === 'GET') {
    const item = Object.values(evidenceByPage).flat().find((e) => e.id === m[1])
    if (!item) return json(res, 404, { detail: 'evidence 不存在' })
    return json(res, 200, { ...item, observations: [] })
  }

  return json(res, 404, { detail: `mock 未实现: ${method} ${path}` })
}

const server = http.createServer(handle)
server.listen(PORT, '127.0.0.1', () => {
  console.log(`phase8b structured-wiki mock listening on ${PORT}`)
})
