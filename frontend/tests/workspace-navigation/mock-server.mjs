// Phase 8A.1 浏览器验收：隔离请求 mock（只读内存数据，零数据库写入）。
//
// 监听 MOCK_PORT（默认 8001），模拟认证 + 工作区 + wiki 只读接口，附带：
//   - 请求日志：GET /__log、清空 GET /__reset-log
//   - 动态控制：POST /__control { ... }，字段见 control 定义
// 由 accept.mjs 作为独立子进程启动；不依赖任何正式后端/数据库/模型。
import http from 'node:http'
import { URL } from 'node:url'

const PORT = Number(process.env.MOCK_PORT || 8001)

const users = {
  'tok-admin': { id: 'u-admin', username: 'admin', display_name: '管理员', email: 'admin@test', is_local: true, groups: ['__local_admin__'], is_admin: true },
  'tok-both': { id: 'u-both', username: 'both', display_name: '双区用户', email: 'both@test', is_local: true, groups: ['engineering', 'sales'], is_admin: false, is_wiki_editor: true },
  'tok-eng': { id: 'u-eng', username: 'eng', display_name: '工程用户', email: 'eng@test', is_local: true, groups: ['engineering'], is_admin: false, is_wiki_editor: true },
  'tok-sales': { id: 'u-sales', username: 'sales', display_name: '销售用户', email: 'sales@test', is_local: true, groups: ['sales'], is_admin: false },
  'tok-qa': { id: 'u-qa', username: 'qa', display_name: 'QA 用户', email: 'qa@test', is_local: true, groups: ['qa'], is_admin: false },
}

const now = '2026-09-04T09:00:00.000000'

const workspaces = [
  { id: 'ws-eng', key: 'ws_key_eng', name: '工程工作区', description: '研发工程主题', acl_scope: '{"groups":["engineering"]}', scope_id: 'group:engineering', status: 'active', created_by: 'u-admin', created_at: now, updated_at: now },
  { id: 'ws-sales', key: 'ws_key_sales', name: '销售工作区', description: '销售主题', acl_scope: '{"groups":["sales"]}', scope_id: 'group:sales', status: 'active', created_by: 'u-admin', created_at: now, updated_at: now },
  { id: 'ws-arch', key: 'ws_key_arch', name: '归档工程区', description: '已归档', acl_scope: '{"groups":["engineering"]}', scope_id: 'group:engineering', status: 'archived', created_by: 'u-admin', created_at: now, updated_at: now },
]

const bindings = [
  { binding_id: 'b-eng-1', workspace_id: 'ws-eng', notebook_id: 'nb-eng', notebook_name: '工程知识库', status: 'active', created_at: now, updated_at: now },
  { binding_id: 'b-eng-2', workspace_id: 'ws-eng', notebook_id: 'nb-legacy', notebook_name: '工程遗留库', status: 'active', created_at: now, updated_at: now },
  { binding_id: 'b-eng-3', workspace_id: 'ws-eng', notebook_id: 'nb-disabled', notebook_name: '已解绑旧库', status: 'disabled', created_at: now, updated_at: now },
  { binding_id: 'b-sales-1', workspace_id: 'ws-sales', notebook_id: 'nb-sales', notebook_name: '销售知识库', status: 'active', created_at: now, updated_at: now },
]

function makeCitation(id, evidenceId, preview) {
  return { id, evidence: { id: evidenceId, evidence_type: 'text', preview, page_id: 'p-eng-1', page_title: '水箱安装指导(原始)', source_url: null } }
}
function makeSection(id, section_type, heading, content, citations) {
  return {
    id, section_type, heading, content, locked: false, citations,
    version_label: 'v1.2', is_common: false, content_origin: 'compiled', merge_policy: 'auto',
    version_status: 'current', diff_notice: null,
  }
}
function makePage(id, workspace_id, title, summary, category, status, keywords, sections) {
  return {
    id, title, summary, status, category, workspace_id, keywords, sections,
    locked: false, latest_version: 'v1.2', current_revision_id: 'r1', preview_revision_id: null,
    has_preview: false, updated_at: '2026-09-03T10:00:00',
  }
}

const wikiDb = {
  'ws-eng': [
    makePage('p-eng-1', 'ws-eng', '水箱安装手册', '工程-水箱安装总览', '安装', 'published', ['水箱', '安装', '连接'], [
      makeSection('s-eng-1', 'summary', '主题概览', '本手册说明水箱安装整体流程与验收要点。', [makeCitation('c-eng-1', 'e-eng-1', '安装指导原文节选……')]),
      makeSection('s-eng-2', 'steps', '操作步骤', '1) 固定底座 2) 连接进出水口。', []),
    ]),
    makePage('p-eng-2', 'ws-eng', '接线规范', '工程-电气接线要求', '电气', 'published', ['接线', '电压'], [
      makeSection('s-eng-3', 'rules', '规则与约束', '接线须断电操作，线径按电流选择。', []),
      makeSection('s-eng-4', 'steps', '操作步骤', '端子排布按图纸顺序。', []),
    ]),
    makePage('p-eng-3', 'ws-eng', '水箱调试(草稿)', '工程-调试草稿', '调试', 'draft', [], [
      makeSection('s-eng-5', 'steps', '操作步骤', '调试未完成，待发布。', []),
    ]),
  ],
  'ws-sales': [
    makePage('p-sales-1', 'ws-sales', '报价方案模板', '销售-报价模板说明', '报价', 'published', ['报价'], [
      makeSection('s-sales-1', 'summary', '主题概览', '标准报价表填写方法与折扣规则。', []),
    ]),
    makePage('p-sales-2', 'ws-sales', '客户跟进话术', '销售-话术', '话术', 'published', ['话术'], [
      makeSection('s-sales-2', 'facts', '关键事实', '跟进频率与关键节点。', []),
    ]),
  ],
}

const revisionsDb = {
  'p-eng-1': [
    { id: 'r1', wiki_page_id: 'p-eng-1', parent_revision_id: null, title: '水箱安装手册', summary: 'A初版', status: 'published', created_at: '2026-08-01T09:00:00' },
    { id: 'r2', wiki_page_id: 'p-eng-1', parent_revision_id: 'r1', title: '水箱安装手册', summary: 'A补丁', status: 'published', created_at: '2026-08-20T09:00:00' },
    { id: 'r3', wiki_page_id: 'p-eng-1', parent_revision_id: 'r2', title: '水箱安装手册', summary: 'A草稿', status: 'draft', created_at: '2026-09-02T09:00:00' },
  ],
  'p-eng-2': [
    { id: 'r1', wiki_page_id: 'p-eng-2', parent_revision_id: null, title: '接线规范', summary: 'B初版', status: 'published', created_at: '2026-08-03T09:00:00' },
  ],
}

const diffOf = {
  'p-eng-1': {
    added: [{ section_type: 'steps', heading: '操作步骤', new_content: 'A页Diff内容' }],
    removed: [],
    changed: [{ section_type: 'summary', heading: '主题概览', old_content: '旧', new_content: '新' }],
  },
  'p-eng-2': {
    added: [{ section_type: 'rules', heading: '规则与约束', new_content: 'B页Diff内容' }],
    removed: [],
    changed: [],
  },
}

const evidenceByPage = {
  'p-eng-1': [
    { id: 'e-eng-1', evidence_type: 'text', content: '原始文档节选：安装时必须使用扭矩扳手……', locator: { page_number: 3, heading: '安装' }, content_hash: null, source_doc_hash: null, extraction_method: 'text', model_name: null, confidence: 0.9, needs_review: false, status: 'active', source: { page_id: 'src-page-9', title: '水箱安装指导(原始)', chunk_id: null } },
  ],
}
const evidenceDetail = {
  'e-eng-1': { ...evidenceByPage['p-eng-1'][0], observations: [] },
}

const control = {
  errorWorkspaces: false,
  slowWorkspace: '',
  slowRevisionsPage: '',
  slowDiffPage: '',
  slowPatchPage: '',
  // 详情响应 workspace_id 篡改：{ [pageId]: 'other' | 'empty' | 'null' | 'absent' }
  detailTamper: {},
}
const requestLog = []

function serializePagePublic(p, admin) {
  const base = { ...p }
  delete base.keywords
  delete base.sections
  return { ...base, preview_revision_id: admin ? p.preview_revision_id : null }
}
function serializeWorkspace(w, admin) {
  if (!admin) return { id: w.id, name: w.name, description: w.description, status: w.status, created_at: w.created_at, updated_at: w.updated_at }
  return { ...w }
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
function userKey(req) {
  const m = /Bearer\s+(\S+)/.exec(req.headers.authorization || '')
  return (m && m[1]) || 'anon'
}

function json(res, code, obj) {
  const body = JSON.stringify(obj)
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8' })
  res.end(body)
}
function delay(ms) {
  return new Promise((r) => setTimeout(r, ms))
}

async function handle(req, res) {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  const path = url.pathname
  const method = req.method

  if (path === '/__control') {
    let body = ''
    for await (const chunk of req) body += chunk
    Object.assign(control, JSON.parse(body || '{}'))
    return json(res, 200, { ok: true, control })
  }
  if (path === '/__log') return json(res, 200, { log: requestLog })
  if (path === '/__reset-log') {
    requestLog.length = 0
    return json(res, 200, { ok: true })
  }

  const user = userOf(req)
  if (!user) return json(res, 401, { detail: '未认证' })
  requestLog.push({ user: user.username, token: userKey(req), method, path, ws: url.searchParams.get('workspace_id') || '' })

  if (path === '/api/auth/me') return json(res, 200, user)
  if (path === '/api/p7/flags') return json(res, 200, { flags: { wiki_topic_enabled: true } })

  if (path === '/api/wiki-workspaces' && method === 'GET') {
    if (control.errorWorkspaces) return json(res, 500, { detail: 'mock workspace list internal error' })
    const admin = !!user.is_admin
    return json(res, 200, { workspaces: visibleWorkspaces(user).map((w) => serializeWorkspace(w, admin)) })
  }
  let m = path.match(/^\/api\/wiki-workspaces\/([^/]+)\/notebooks$/)
  if (m && method === 'GET') {
    if (!user.is_admin) return json(res, 403, { detail: '仅管理员可执行此操作' })
    const ws = workspaces.find((w) => w.id === m[1])
    if (!ws) return json(res, 404, { detail: '工作区不存在' })
    const list = bindings.filter((b) => b.workspace_id === ws.id && b.status === 'active')
    return json(res, 200, { workspace_id: ws.id, workspace_name: ws.name, bindings: list })
  }
  m = path.match(/^\/api\/wiki-workspaces\/([^/]+)\/wikis$/)
  if (m && method === 'GET') {
    const ws = visibleWorkspaces(user).find((w) => w.id === m[1])
    if (!ws) return json(res, 404, { detail: '工作区不存在' })
    const rows = wikiDb[m[1]] || []
    const items = rows.filter((p) => user.is_admin || p.status === 'published').map((p) => serializePagePublic(p, !!user.is_admin))
    return json(res, 200, { workspace_id: ws.id, wikis: items })
  }
  m = path.match(/^\/api\/wiki-workspaces\/([^/]+)$/)
  if (m && method === 'GET') {
    const ws = visibleWorkspaces(user).find((w) => w.id === m[1])
    if (!ws) return json(res, 404, { detail: '工作区不存在' })
    return json(res, 200, serializeWorkspace(ws, !!user.is_admin))
  }

  if (path === '/api/wiki' && method === 'GET') {
    const wsId = url.searchParams.get('workspace_id') || ''
    const rows = wikiDb[wsId] || []
    const admin = !!user.is_admin
    let pages = rows.filter((p) => (admin ? p.status !== 'archived' : p.status === 'published'))
    const q = (url.searchParams.get('q') || '').trim().toLowerCase()
    if (q) pages = pages.filter((p) => `${p.title}${p.summary}${p.keywords.join('')}`.toLowerCase().includes(q))
    const category = url.searchParams.get('category')
    if (category) pages = pages.filter((p) => p.category === category)
    const status = url.searchParams.get('status')
    if (admin && status) pages = pages.filter((p) => p.status === status)
    const items = pages.map((p) => serializePagePublic(p, admin))
    if (control.slowWorkspace === wsId) {
      await delay(1600)
      return json(res, 200, { pages: items })
    }
    return json(res, 200, { pages: items })
  }
  m = path.match(/^\/api\/wiki\/([^/]+)\/revisions$/)
  if (m && method === 'GET') {
    const pid = m[1]
    const revs = (revisionsDb[pid] || []).filter((r) => user.is_admin || r.status === 'published')
    if (control.slowRevisionsPage === pid) await delay(1800)
    return json(res, 200, { revisions: revs })
  }
  m = path.match(/^\/api\/wiki\/([^/]+)\/diff\/([^/]+)$/)
  if (m && method === 'GET') {
    const [, pid] = m
    if (!revisionsDb[pid]?.some((r) => r.id === m[2])) return json(res, 404, { detail: 'Revision 不存在' })
    if (control.slowDiffPage === pid) await delay(1800)
    return json(res, 200, diffOf[pid] || { added: [], removed: [], changed: [] })
  }
  m = path.match(/^\/api\/wiki\/([^/]+)\/revisions\/([^/]+)\/sections\/([^/]+)$/)
  if (m && method === 'PATCH') {
    if (control.slowPatchPage === m[1]) await delay(1800)
    return json(res, 200, { message: 'Section 已保存并生效', locked: true })
  }
  m = path.match(/^\/api\/wiki\/([^/]+)$/)
  if (m && method === 'GET') {
    const page = Object.values(wikiDb).flat().find((p) => p.id === m[1])
    if (!page || (!user.is_admin && page.status !== 'published')) return json(res, 404, { detail: '主题页不存在' })
    const admin = !!user.is_admin
    const detail = {
      ...serializePagePublic(page, admin),
      sections: (page.sections || []).map((s) => ({ ...s })),
      related_topics: [],
      related_topic_ids: [],
      viewing_revision_id: 'r1',
    }
    // 注入篡改（用于前端“详情工作区严格匹配”负向用例）
    const tamper = control.detailTamper && control.detailTamper[page.id]
    if (tamper === 'other') detail.workspace_id = 'ws-sales'
    else if (tamper === 'empty') detail.workspace_id = ''
    else if (tamper === 'null') detail.workspace_id = null
    else if (tamper === 'absent') delete detail.workspace_id
    return json(res, 200, detail)
  }

  if (path === '/api/evidence' && method === 'GET') {
    const pid = url.searchParams.get('page_id')
    return json(res, 200, { evidence: pid ? (evidenceByPage[pid] || []) : [] })
  }
  m = path.match(/^\/api\/evidence\/([^/]+)$/)
  if (m && method === 'GET') {
    const item = evidenceDetail[m[1]]
    if (!item) return json(res, 404, { detail: 'evidence 不存在' })
    return json(res, 200, item)
  }

  return json(res, 404, { detail: `mock 未实现: ${method} ${path}` })
}

const server = http.createServer(handle)
server.listen(PORT, '127.0.0.1', () => {
  console.log(`phase8a.1 mock listening on ${PORT}`)
})
