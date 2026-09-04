// Phase 8C 章节 Evidence / 编辑者诊断 / 管理员编译任务面板：浏览器验收隔离 mock。
//
// 内存只读数据，零 DB/模型/正式后端。数据与 docs/phase-8c-contract.md 逐字段对齐：
//   - 章节 Evidence（分页 offset+limit / 截断 / 多 binding 聚合 / state 派生；total 仅授权后可见计数）
//   - 编辑者只读诊断（GET .../diagnostics?revision_id=；缺省 current；skill 无 selection，
//     selected_by ∈ {auto,manual,migration,default_fallback,locked,sticky}|null；reason_code 受控码或 "unknown"；
//     validation.sections 来自该 revision）
//   - wiki-compile runs list（workspace_id 过滤 + 分页）/ get（stages 随真实状态派生）/ retry（含 409）/ cancel（状态流）
//
// 附带验收控制：
//   POST /__control { ... }           动态延迟/错误/状态覆盖
//   GET  /__log、GET /__reset-log     请求日志
//   GET  /__runs-timeline、GET /__reset-runs-timeline  轮询时间窗口（start/end，非重叠检查）
import http from 'node:http'
import { createHash } from 'node:crypto'
import { URL } from 'node:url'

const PORT = Number(process.env.MOCK_PORT || 8001)

function hashOf(content) {
  return createHash('sha256').update(String(content || ''), 'utf8').digest('hex')
}

const users = {
  'tok-reader': { id: 'u-reader', username: 'reader', display_name: '普通读者', email: 'reader@test', is_local: true, groups: ['engineering'], is_admin: false, is_wiki_editor: false },
  'tok-editor': { id: 'u-editor', username: 'editor', display_name: '编辑用户', email: 'editor@test', is_local: true, groups: ['engineering'], is_admin: false, is_wiki_editor: true },
  'tok-admin': { id: 'u-admin', username: 'admin', display_name: '管理员', email: 'admin@test', is_local: true, groups: ['engineering', '__local_admin__'], is_admin: true, is_wiki_editor: true },
}

const now = '2026-09-04T09:00:00.000000'
const workspaces = [
  { id: 'ws-eng', key: 'ws_key_eng', name: '工程工作区', description: '工程主题', acl_scope: '{"groups":["engineering"]}', scope_id: 'group:engineering', status: 'active', created_by: 'u-admin', created_at: now, updated_at: now },
  { id: 'ws-sales', key: 'ws_key_sales', name: '销售工作区', description: '销售主题', acl_scope: '{"groups":["sales"]}', scope_id: 'group:sales', status: 'active', created_by: 'u-admin', created_at: now, updated_at: now },
]

const bindingsDb = [
  { binding_id: 'b-eng-1', workspace_id: 'ws-eng', notebook_id: 'nb-eng', notebook_name: '工程知识库', status: 'active', created_at: now, updated_at: now },
  { binding_id: 'b-sales-1', workspace_id: 'ws-sales', notebook_id: 'nb-sales', notebook_name: '销售知识库', status: 'active', created_at: now, updated_at: now },
]

// ===================== Wiki 页面与章节 =====================

function makeSection({ id, section_type, heading, content, display = null, section_role = null, version_label = null }) {
  return { id, section_type, heading, content, locked: false, citations: [], version_label, is_common: false, content_origin: 'compiled', merge_policy: 'auto', version_status: 'current', diff_notice: null, section_role, display }
}

// 复刻 8B 结构化 endpoint 展示（回归验证：新“章节证据”入口不破坏结构化正文）
const sEpContent = '## GET /users（v1）\n\n分页获取用户列表。'
const sEpDisplay = {
  schema_version: 'api-section-display/v2',
  section_role: 'endpoint',
  content_hash: hashOf(sEpContent),
  version_scope: 'v1',
  endpoint: { method: 'GET', path: '/users', summary: '获取用户列表', description: '分页返回用户列表，可按名称过滤。' },
  parameters: [
    { location: 'query', name: 'page', required: false, description: '页码，从 1 开始。', type: 'integer' },
  ],
  request_body: null,
  responses: [
    { status_code: '200', description: '成功返回用户列表', media_types: [{ media_type: 'application/json', schema_status: 'present' }] },
  ],
  error_codes: [{ code: 'RATE_LIMITED', description: '请求过于频繁', http_status: '' }],
  examples: [],
  version_notes: [{ version_scope: 'v1', note: 'v1 初始版本。' }],
  knowledge_gaps: [],
  conflicts: [],
}
const sEp = makeSection({ id: 'p-main-ep', section_type: 'endpoint', heading: 'GET /users（v1）', version_label: 'v1', content: sEpContent, section_role: 'endpoint', display: sEpDisplay })

const pMain = {
  id: 'p-main',
  workspace_id: 'ws-eng',
  title: '接口主手册',
  summary: '主手册总览（含 Evidence 追溯验收章节）。',
  status: 'published',
  category: '综合主题',
  keywords: ['主手册', '接口'],
  locked: false,
  latest_version: 'v1.0',
  current_revision_id: 'r1',
  preview_revision_id: null,
  has_preview: false,
  updated_at: '2026-09-03T10:00:00',
  sections: [
    makeSection({ id: 'p-main-ov', section_type: 'overview', heading: '主题概览', content: '# 接口主手册\n\n本手册包含章节证据追溯的验收样例。' }),
    sEp,
    makeSection({ id: 'p-main-a', section_type: 'steps', heading: '第一节', content: '# 第一节\n\n安装要求参考来源原文。' }),
    makeSection({ id: 'p-main-b', section_type: 'facts', heading: '第二节', content: '# 第二节\n\n接线要求与调试约定。' }),
    makeSection({ id: 'p-main-states', section_type: 'facts', heading: '状态样例', content: '用于验证 stale/rejected/hash changed/截断提示。' }),
    makeSection({ id: 'p-main-lots', section_type: 'facts', heading: '长列表样例', content: '超过一页的分页验证（含隐藏来源不计数）。' }),
    makeSection({ id: 'p-main-empty', section_type: 'facts', heading: '空样例', content: '该章节无可见 Evidence。' }),
    makeSection({ id: 'p-main-fail', section_type: 'facts', heading: '失败样例', content: '该章节 Evidence 请求可注入失败。' }),
  ],
}

const pOther = {
  id: 'p-other',
  workspace_id: 'ws-eng',
  title: '另一手册',
  summary: '跨页面竞态切换的落点页。',
  status: 'published',
  category: '综合主题',
  keywords: ['另一手册'],
  locked: false,
  latest_version: 'v1.0',
  current_revision_id: 'r1',
  preview_revision_id: null,
  has_preview: false,
  updated_at: '2026-09-03T10:00:00',
  sections: [
    makeSection({ id: 'p-other-ov', section_type: 'overview', heading: '主题概览', content: '# 另一手册\n\n用于迟到请求不覆盖断言。' }),
    makeSection({ id: 'p-other-h', section_type: 'steps', heading: '章节H', content: '# 章节H\n\n跨页竞态目标。' }),
  ],
}

// V1/V2 诊断验收页：同一页面拥有 published（current）与 preview（draft）两种 revision，
// 预览/历史模拟通过既有“编辑→保存”真实 UI 流程切入（保存后进入 preview revision 视图）。
function makeDraftPage({ id, title, category, currentRev, draftRev, currentHeading, draftHeading }) {
  return {
    id,
    workspace_id: 'ws-eng',
    title,
    summary: `${title}：用于 diagnostics 按 revision 刷新验收。`,
    status: 'published',
    category,
    keywords: [title],
    locked: false,
    latest_version: 'v1.0',
    current_revision_id: currentRev,
    preview_revision_id: draftRev,
    has_preview: true,
    updated_at: '2026-09-03T11:00:00',
    sections: [
      makeSection({ id: `${id}-s1`, section_type: 'steps', heading: currentHeading, content: `# ${currentHeading}\n\n[${id}-CURRENT-MARK] published 正文。` }),
    ],
    previewSections: [
      makeSection({ id: `${id}-s1`, section_type: 'steps', heading: draftHeading, content: `# ${draftHeading}\n\n[${id}-DRAFT-MARK] preview 草稿正文。` }),
    ],
  }
}

const pV1 = makeDraftPage({ id: 'p-v1', title: '版本诊断甲', category: '诊断验收', currentRev: 'r-v1a', draftRev: 'r-v1draft', currentHeading: '版本甲章节', draftHeading: '版本乙章节' })
const pV2 = makeDraftPage({ id: 'p-v2', title: '版本诊断乙', category: '诊断验收', currentRev: 'r-v2c', draftRev: 'r-v2draft', currentHeading: '稳定章节', draftHeading: '草稿章节' })

const wikiDb = { 'ws-eng': [pMain, pOther, pV1, pV2], 'ws-sales': [] }

function serializeWorkspace(w) {
  return { id: w.id, name: w.name, description: w.description, status: w.status, created_at: w.created_at, updated_at: w.updated_at }
}
function visibleWorkspaces(user) {
  if (user.is_admin) return workspaces.filter((w) => w.status === 'active')
  return workspaces.filter((w) => {
    if (w.status !== 'active') return false
    const groups = w.scope_id.startsWith('group:') ? w.scope_id.slice(6).split(',') : []
    return groups.some((g) => user.groups.includes(g))
  })
}
function serializeSection(s) {
  return { ...s }
}
function serializePageList(p) {
  const { keywords, sections, previewSections, ...rest } = p
  return { ...rest }
}

// ===================== 章节 Evidence 数据 =====================

function makeEv({ id, type = 'text', status, hash, state, content, locator, source, bindings }) {
  return { evidence_id: id, evidence_type: type, status, hash_matches: hash, state, content, content_truncated: false, locator, source_display_name: source, bindings }
}

// 分页数据集：底层 26 条，其中 2 条来源对当前用户不可见 → 不返回也不计入 total（mock 真实契约）。
const paginatedRaw = []
for (let i = 1; i <= 26; i++) {
  const pad = String(i).padStart(2, '0')
  paginatedRaw.push(makeEv({
    id: `ev-page-${pad}`,
    status: 'active', hash: true, state: 'active_current',
    content: `# 第 ${i} 条\n\nEVID-PAGE-${pad} 分页正文第 ${i} 条。`,
    locator: { page_number: i, heading: '长列表', image_id: null, content_type: null },
    source: i > 24 ? '隐藏来源(无权)' : '分页来源(可见)',
    bindings: [{ field_path: `page.item_${pad}`, usage_type: 'support' }],
  }))
}
const HIDDEN_EVIDENCE_IDS = new Set(['ev-page-25', 'ev-page-26'])

const evidenceBySection = {
  'p-main|p-main-a': [
    makeEv({
      id: 'ev-a-1', status: 'active', hash: true, state: 'active_current',
      content: '# A 节依据\n\n安装说明原文：[EVID-A-MARK] 使用扭矩扳手按 25 N·m 固定。',
      locator: { page_number: 5, heading: '安装', image_id: null, content_type: null },
      source: '水箱安装指导(原始)',
      bindings: [
        { field_path: 'guide.install.section_a', usage_type: 'support' },
        { field_path: 'guide.torque.level', usage_type: 'conflict' },
      ],
    }),
  ],
  'p-main|p-main-b': [
    makeEv({
      id: 'ev-b-1', status: 'active', hash: true, state: 'active_current',
      content: '接线前须断电。原文标记 [EVID-B-MARK]。',
      locator: { page_number: 9, heading: '调试', image_id: null, content_type: null },
      source: '电气接线规范(原始)',
      bindings: [{ field_path: 'wiring.safety', usage_type: 'support' }],
    }),
  ],
  'p-main|p-main-states': [
    makeEv({
      id: 'ev-states-changed', status: 'active', hash: false, state: 'changed',
      content: '绑定快照与当前 Evidence 正文不一致的示例。',
      locator: { page_number: 1, heading: null, image_id: null, content_type: null },
      source: '变更示例来源',
      bindings: [{ field_path: 'hash.changed', usage_type: 'support' }],
    }),
    makeEv({
      id: 'ev-states-stale', status: 'stale', hash: false, state: 'stale',
      content: '该条 Evidence 已过期，仅供历史参考。',
      locator: { page_number: 2, heading: null, image_id: null, content_type: null },
      source: '过期来源',
      bindings: [{ field_path: 'stale.example', usage_type: 'support' }],
    }),
    makeEv({
      id: 'ev-states-rejected', status: 'rejected', hash: false, state: 'rejected',
      content: '该条 Evidence 已被否决，不判对错。',
      locator: { page_number: 3, heading: null, image_id: null, content_type: null },
      source: '否决来源',
      bindings: [{ field_path: 'rejected.example', usage_type: 'conflict' }],
    }),
    makeEv({
      id: 'ev-states-long', status: 'active', hash: true, state: 'active_current',
      content: 'EV-LONG-MARK\n' + '内容很长时服务端会截断至 2000 字符并置 content_truncated。'.repeat(120),
      locator: { page_number: 4, heading: null, image_id: null, content_type: null },
      source: '长文本来源',
      bindings: [{ field_path: 'long.example', usage_type: 'support' }],
    }),
  ],
  'p-main|p-main-lots': paginatedRaw,
  'p-main|p-main-fail': [
    makeEv({
      id: 'ev-fail-1', status: 'active', hash: true, state: 'active_current',
      content: '失败样例正文：[EV-FAIL-MARK] 仅在未注入错误时可见。',
      locator: { page_number: 7, heading: null, image_id: null, content_type: null },
      source: '失败样例来源',
      bindings: [{ field_path: 'fail.example', usage_type: 'support' }],
    }),
  ],
  'p-other|p-other-h': [
    makeEv({
      id: 'ev-h-1', status: 'active', hash: true, state: 'active_current',
      content: '另一份手册的章节依据：[EVID-H-MARK]。',
      locator: { page_number: 2, heading: '章节H', image_id: null, content_type: null },
      source: '另一来源(原始)',
      bindings: [{ field_path: 'other.example', usage_type: 'support' }],
    }),
  ],
}

// ===================== 编辑者诊断（真实 selected_by / reason_code 枚举；无 skill.selection） =====================

const API_REF_SKILL = {
  key: 'api_reference',
  display_name: 'API Reference',
  version: '1',
  selected_by: 'auto',
  locked: false,
  reason_code: 'DETERMINISTIC_HIGH_CONFIDENCE',
}

// revs[rev] === null → diagnostics 对该 revision 返回 404（统一“无权查看该版本”语义）。
const diagnosticsDb = {
  'p-main': {
    current: 'r1',
    revs: {
      r1: {
        is_current_wiki_config: true,
        skill: { ...API_REF_SKILL },
        validation: {
          summary: 'pass',
          sections: [
            { heading: '主题概览', validation_status: 'pass' },
            { heading: 'GET /users（v1）', validation_status: 'pass' },
            { heading: '状态样例', validation_status: 'unknown' },
          ],
        },
      },
    },
  },
  'p-v1': {
    current: 'r-v1a',
    revs: {
      'r-v1a': {
        is_current_wiki_config: true,
        skill: { ...API_REF_SKILL },
        validation: {
          summary: 'pass',
          sections: [
            { heading: '主题概览', validation_status: 'pass' },
            { heading: '版本甲接口', validation_status: 'pass' },
          ],
        },
      },
      'r-v1draft': {
        is_current_wiki_config: true,
        skill: { key: 'api_reference', display_name: 'API Reference', version: '1', selected_by: 'manual', locked: true, reason_code: 'MANUAL_OVERRIDE' },
        validation: {
          summary: 'fail',
          sections: [
            { heading: '主题概览', validation_status: 'pass' },
            { heading: '版本乙接口', validation_status: 'unknown' },
            { heading: '新增草稿章节', validation_status: 'fail' },
          ],
        },
      },
    },
  },
  'p-v2': {
    current: 'r-v2c',
    revs: {
      'r-v2c': {
        is_current_wiki_config: true,
        skill: { key: 'api_reference', display_name: 'API Reference', version: '1', selected_by: null, locked: false, reason_code: 'unknown' },
        validation: {
          summary: 'unknown',
          sections: [
            { heading: '主题概览', validation_status: 'unknown' },
            { heading: '稳定版校验章', validation_status: 'pass' },
          ],
        },
      },
      'r-v2draft': null, // 404：无权查看该版本
    },
  },
}

// ===================== 编译任务（runs） =====================

function runRow(id, { pipeline_key, pipeline_version, trigger_type, workspace_id, status, attempt = 1, max_attempts = 3, cancel_requested = false, safe_error_code = null, safe_error_message = null, error_summary = null, created, started = null, finished = null, current_stage = null, output_revision_id = null }) {
  return {
    id, pipeline_key, pipeline_version, trigger_type, trigger_object_id: null, source_sync_run_id: null,
    workspace_id, wiki_page_id: null, status, current_stage, output_revision_id,
    error_summary, safe_error_code, safe_error_message,
    attempt, max_attempts, cancel_requested,
    created_at: created, started_at: started, finished_at: finished, heartbeat_at: null,
  }
}

const baseRuns = {
  'cr-poll': runRow('cr-poll', { pipeline_key: 'notes_wiki', pipeline_version: '1', trigger_type: 'auto', workspace_id: 'ws-eng', status: 'succeeded', attempt: 1, created: '2026-09-04T09:00:00', started: '2026-09-04T09:00:01', finished: '2026-09-04T09:02:00', current_stage: 'compile', output_revision_id: 'r1' }),
  'cr-fail': runRow('cr-fail', { pipeline_key: 'notes_wiki', pipeline_version: '1', trigger_type: 'manual', workspace_id: 'ws-eng', status: 'failed', attempt: 1, created: '2026-09-04T08:05:00', started: '2026-09-04T08:05:02', finished: '2026-09-04T08:06:40', safe_error_code: 'compile_failed', safe_error_message: '章节编译失败：请检查上游来源（已脱敏）', error_summary: '章节编译失败：请检查上游来源（已脱敏）' }),
  'cr-retry': runRow('cr-retry', { pipeline_key: 'notes_wiki', pipeline_version: '1', trigger_type: 'manual', workspace_id: 'ws-eng', status: 'failed', attempt: 1, created: '2026-09-04T08:03:00', started: '2026-09-04T08:03:02', finished: '2026-09-04T08:04:10', safe_error_code: 'compile_failed', safe_error_message: '重试验收 run 失败（已脱敏）', error_summary: '重试验收 run 失败（已脱敏）' }),
  'cr-done': runRow('cr-done', { pipeline_key: 'notes_wiki', pipeline_version: '1', trigger_type: 'manual', workspace_id: 'ws-eng', status: 'succeeded', attempt: 1, created: '2026-09-04T08:00:00', started: '2026-09-04T08:00:03', finished: '2026-09-04T08:02:10', output_revision_id: 'r1' }),
  'cr-sales': runRow('cr-sales', { pipeline_key: 'notes_wiki', pipeline_version: '1', trigger_type: 'auto', workspace_id: 'ws-sales', status: 'succeeded', attempt: 1, created: '2026-09-04T07:00:00', started: '2026-09-04T07:00:01', finished: '2026-09-04T07:02:00', output_revision_id: 'r1' }),
}
// 工程工作区补齐 > 一页（23 条：4 条验收 + 19 条历史成功，均早于 08:00，避免挤掉 cr-done/cr-fail/cr-poll/cr-retry）
for (let i = 1; i <= 19; i++) {
  const pad = String(i).padStart(2, '0')
  const minutes = 30 + i // 07:31..07:49
  const hh = String(Math.floor(minutes / 60)).padStart(2, '0')
  const mm = String(minutes % 60).padStart(2, '0')
  baseRuns[`cr-x${pad}`] = runRow(`cr-x${pad}`, { pipeline_key: 'notes_wiki', pipeline_version: '1', trigger_type: 'batch', workspace_id: 'ws-eng', status: 'succeeded', attempt: 1, created: `2026-09-04T${hh}:${mm}:00`, started: `2026-09-04T${hh}:${mm}:01`, finished: `2026-09-04T${hh}:${mm + 1 > 59 ? 59 : mm + 1}:00`, output_revision_id: 'r1' })
}

// 生效状态覆盖（__control.runStates 写入；cancel/retry 也写此表：status/cancel_requested/attempt/current_stage）
const liveStates = {}

function effectiveRun(id) {
  const live = liveStates[id]
  const base = baseRuns[id]
  if (!base) return null
  const merged = { ...base }
  if (live) {
    if (live.status !== undefined) merged.status = live.status
    if (typeof live.cancel_requested === 'boolean') merged.cancel_requested = live.cancel_requested
    if (live.attempt !== undefined) merged.attempt = live.attempt
    if (live.current_stage !== undefined) merged.current_stage = live.current_stage
    if (live.finished_at !== undefined) merged.finished_at = live.finished_at
    if (live.started_at !== undefined) merged.started_at = live.started_at
  }
  return merged
}

function serializeRun(run) {
  return {
    id: run.id,
    pipeline_key: run.pipeline_key,
    pipeline_version: run.pipeline_version,
    trigger_type: run.trigger_type,
    trigger_object_id: run.trigger_object_id,
    source_sync_run_id: run.source_sync_run_id,
    workspace_id: run.workspace_id,
    wiki_page_id: run.wiki_page_id,
    status: run.status,
    current_stage: run.current_stage,
    output_revision_id: run.output_revision_id,
    error_summary: run.error_summary,
    safe_error_code: run.safe_error_code,
    safe_error_message: run.safe_error_message,
    attempt: run.attempt,
    max_attempts: run.max_attempts,
    cancel_requested: run.cancel_requested,
    created_at: run.created_at,
    started_at: run.started_at,
    finished_at: run.finished_at,
    heartbeat_at: run.heartbeat_at,
  }
}

const staticStages = {
  'cr-done': [
    { id: 'st-done-1', run_id: 'cr-done', stage_key: 'extract', stage_order: 1, status: 'succeeded', attempt: 1, retryable: true, component_key: 'notes_extractor', component_version: '1.0', parent_stage_run_id: null, error_code: '', error_message: '', safe_error_code: null, safe_error_message: null, metrics_summary: { cached: false }, started_at: '2026-09-04T08:00:05', finished_at: '2026-09-04T08:00:40', created_at: '2026-09-04T08:00:04' },
    { id: 'st-done-2', run_id: 'cr-done', stage_key: 'compile', stage_order: 2, status: 'succeeded', attempt: 1, retryable: true, component_key: 'notes_compiler', component_version: '1.0', parent_stage_run_id: null, error_code: '', error_message: '', safe_error_code: null, safe_error_message: null, metrics_summary: {}, started_at: '2026-09-04T08:00:41', finished_at: '2026-09-04T08:02:10', created_at: '2026-09-04T08:00:40' },
  ],
  'cr-fail': [
    { id: 'st-fail-1', run_id: 'cr-fail', stage_key: 'extract', stage_order: 1, status: 'failed', attempt: 1, retryable: true, component_key: 'notes_extractor', component_version: '1.0', parent_stage_run_id: null, error_code: 'compile_failed', error_message: '章节编译失败（已脱敏）', safe_error_code: 'compile_failed', safe_error_message: '章节编译失败（已脱敏）', metrics_summary: {}, started_at: '2026-09-04T08:05:02', finished_at: '2026-09-04T08:05:20', created_at: '2026-09-04T08:05:01' },
  ],
}

// 无静态 stage 的 run（cr-poll / cr-retry 等）：按实时 status/attempt/current_stage 派生 stage 时间线。
function derivedStages(run) {
  const attempt = Number(run.attempt) || 1
  const T0 = '2026-09-04T09:00:02'
  const mk = (stageKey, order, status, started, finished, safeError = null) => ({
    id: `${run.id}-${stageKey}-a${attempt}`,
    run_id: run.id,
    stage_key: stageKey,
    stage_order: order,
    status,
    attempt,
    retryable: true,
    component_key: 'notes_wiki_skill_v3',
    component_version: '1.0',
    parent_stage_run_id: null,
    error_code: status === 'failed' ? 'compile_failed' : '',
    error_message: status === 'failed' ? (safeError || '章节编译失败（已脱敏）') : '',
    safe_error_code: status === 'failed' ? 'compile_failed' : null,
    safe_error_message: status === 'failed' ? (safeError || '章节编译失败（已脱敏）') : null,
    metrics_summary: {},
    started_at: started,
    finished_at: finished,
    created_at: started || T0,
  })
  const st = run.status
  if (st === 'succeeded') {
    return [
      mk('extract', 1, 'succeeded', T0, '2026-09-04T09:00:40'),
      mk('compile', 2, 'succeeded', '2026-09-04T09:00:41', '2026-09-04T09:02:00'),
    ]
  }
  if (st === 'running') {
    const rows = [mk('extract', 1, 'succeeded', T0, '2026-09-04T09:00:40')]
    rows.push(mk('compile', 2, 'running', '2026-09-04T09:00:41', null))
    return rows
  }
  if (st === 'queued') {
    return [mk('extract', 1, 'queued', null, null)]
  }
  if (st === 'failed') {
    return [mk('extract', 1, 'failed', T0, '2026-09-04T09:00:20', run.safe_error_message)]
  }
  if (st === 'cancelled') {
    return [mk('extract', 1, 'cancelled', T0, '2026-09-04T09:00:20')]
  }
  return []
}

function stagesOf(run) {
  if (staticStages[run.id]) return staticStages[run.id]
  return derivedStages(run)
}

// ===================== 控制 =====================

const control = {
  errorWorkspaces: false,
  detailTamper: {},
  slowEvidenceSection: '',
  errorEvidenceSection: '',
  slowRunsMs: 0,
  slowRunsWs: '',
  retry409: false,
  slowRetryMs: 0,
  slowDiagRevision: '',
  slowDiagMs: 1500,
  errorDiagRevision: '',
  runStates: {},
}
const requestLog = []
const runsTimeline = []
let timelineId = 0

// ===================== 基础设施 =====================

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
function sleep(ms) {
  return delay(ms)
}

function serializeEvidenceItem(raw) {
  let content = String(raw.content == null ? '' : raw.content)
  let truncated = false
  if (content.length > 2000) {
    content = content.slice(0, 2000)
    truncated = true
  }
  return {
    evidence_id: raw.evidence_id,
    evidence_type: raw.evidence_type,
    status: raw.status,
    hash_matches: raw.hash_matches,
    state: raw.state,
    content,
    content_truncated: truncated || raw.content_truncated,
    locator: raw.locator || { page_number: null, heading: null, image_id: null, content_type: null },
    source_display_name: raw.source_display_name,
    bindings: raw.bindings || [],
  }
}

function pageById(id) {
  return Object.values(wikiDb).flat().find((p) => p.id === id)
}

async function handle(req, res) {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  const path = url.pathname
  const method = req.method

  if (path === '/__control') {
    let body = ''
    for await (const chunk of req) body += chunk
    const patch = JSON.parse(body || '{}')
    for (const k of Object.keys(control)) {
      if (k === 'runStates') continue
      if (k in patch) control[k] = patch[k]
    }
    if (patch.runStates) Object.assign(liveStates, patch.runStates)
    return json(res, 200, { ok: true, control, liveStates })
  }
  if (path === '/__log') return json(res, 200, { log: requestLog })
  if (path === '/__reset-log') {
    requestLog.length = 0
    return json(res, 200, { ok: true })
  }
  if (path === '/__runs-timeline') return json(res, 200, { entries: runsTimeline })
  if (path === '/__reset-runs-timeline') {
    runsTimeline.length = 0
    timelineId = 0
    return json(res, 200, { ok: true })
  }

  const user = userOf(req)
  if (!user) return json(res, 401, { detail: '未认证' })
  requestLog.push({ user: user.username, token: userKey(req), method, path, ws: url.searchParams.get('workspace_id') || '', at: Date.now() })

  if (path === '/api/auth/me') return json(res, 200, user)

  // ---- 工作区 ----
  if (path === '/api/wiki-workspaces' && method === 'GET') {
    if (control.errorWorkspaces) return json(res, 500, { detail: 'mock 工作区列表异常' })
    return json(res, 200, { workspaces: visibleWorkspaces(user).map(serializeWorkspace) })
  }
  let m = path.match(/^\/api\/wiki-workspaces\/([^/]+)\/notebooks$/)
  if (m && method === 'GET') {
    if (!user.is_admin) return json(res, 403, { detail: '仅管理员可执行此操作' })
    const ws = workspaces.find((w) => w.id === m[1])
    if (!ws) return json(res, 404, { detail: '工作区不存在' })
    return json(res, 200, { workspace_id: ws.id, workspace_name: ws.name, bindings: bindingsDb.filter((b) => b.workspace_id === ws.id && b.status === 'active') })
  }

  // ---- Wiki 目录 ----
  if (path === '/api/wiki' && method === 'GET') {
    const wsId = url.searchParams.get('workspace_id') || ''
    const rows = wikiDb[wsId] || []
    const pages = rows.filter((p) => user.is_admin || p.status === 'published').map(serializePageList)
    return json(res, 200, { pages })
  }

  // ---- 章节 Evidence（8C §1；total 仅授权后可见计数） ----
  m = path.match(/^\/api\/wiki\/([^/]+)\/revisions\/([^/]+)\/sections\/([^/]+)\/evidence$/)
  if (m && method === 'GET') {
    const wikiId = m[1]
    const revisionId = m[2]
    const sectionId = m[3]
    const page = pageById(wikiId)
    const section = page && page.sections.find((s) => s.id === sectionId)
    if (!section) return json(res, 404, { detail: 'Section 不存在' })
    const limit = Math.min(Number(url.searchParams.get('limit')) || 50, 100)
    const offset = Number(url.searchParams.get('offset')) || 0
    const key = `${wikiId}|${sectionId}`
    if (control.slowEvidenceSection === sectionId) await delay(1500)
    if (control.errorEvidenceSection === sectionId) return json(res, 500, { detail: '证据服务异常（mock 注入）' })
    const all = (evidenceBySection[key] || [])
      .filter((raw) => !HIDDEN_EVIDENCE_IDS.has(raw.evidence_id))
      .map(serializeEvidenceItem)
    const items = all.slice(offset, offset + limit)
    return json(res, 200, { wiki_id: wikiId, revision_id: revisionId, section_id: sectionId, total: all.length, limit, offset, items })
  }

  // ---- 章节保存（用于真实 UI “编辑→保存”进入 preview revision 视图的验收模拟） ----
  m = path.match(/^\/api\/wiki\/([^/]+)\/revisions\/([^/]+)\/sections\/([^/]+)$/)
  if (m && method === 'PATCH') {
    const page = pageById(m[1])
    if (!page) return json(res, 404, { detail: '主题页不存在' })
    if (!user.is_admin && !user.is_wiki_editor) return json(res, 403, { detail: '无权编辑该 Wiki' })
    let body = ''
    for await (const chunk of req) body += chunk
    try { JSON.parse(body || '{}') } catch { return json(res, 400, { detail: '请求体非法' }) }
    const inCurrent = page.sections.some((s) => s.id === m[3])
    const inPreview = (page.previewSections || []).some((s) => s.id === m[3])
    if (!inCurrent && !inPreview) return json(res, 404, { detail: 'Section 不存在' })
    return json(res, 200, { message: '已保存', locked: false })
  }

  // ---- 编辑者诊断（8C §2；revision_id 缺省 current；skill 无 selection） ----
  m = path.match(/^\/api\/wiki\/([^/]+)\/diagnostics$/)
  if (m && method === 'GET') {
    if (!user.is_admin && !user.is_wiki_editor) return json(res, 403, { detail: '无权查看编译诊断' })
    const page = pageById(m[1])
    if (!page) return json(res, 404, { detail: '主题页不存在' })
    const rule = diagnosticsDb[page.id]
    if (!rule) return json(res, 404, { detail: 'Revision 不存在或无权查看该版本' })
    const revParam = url.searchParams.get('revision_id') || ''
    const effective = revParam || rule.current
    if (control.errorDiagRevision && control.errorDiagRevision === effective) {
      return json(res, 500, { detail: '诊断服务异常（mock 注入）' })
    }
    if (control.slowDiagRevision && control.slowDiagRevision === effective) {
      await delay(control.slowDiagMs)
    }
    const entry = rule.revs[effective]
    if (entry === undefined || entry === null) {
      return json(res, 404, { detail: 'Revision 不存在或无权查看该版本' })
    }
    return json(res, 200, {
      wiki_id: page.id,
      revision_id: effective,
      editable: true,
      is_current_wiki_config: entry.is_current_wiki_config !== false,
      skill: entry.skill,
      validation: entry.validation,
    })
  }

  // ---- Wiki 详情（preview=true 且页面有草稿 revision → 查看 preview revision；模拟预览/历史视图） ----
  m = path.match(/^\/api\/wiki\/([^/]+)$/)
  if (m && method === 'GET') {
    const page = pageById(m[1])
    if (!page || (!user.is_admin && page.status !== 'published')) return json(res, 404, { detail: '主题页不存在' })
    const list = serializePageList(page)
    const wantPreview = url.searchParams.get('preview') === 'true' && Boolean(page.preview_revision_id) && (user.is_admin || user.is_wiki_editor)
    const detail = {
      ...list,
      preview_revision_id: page.preview_revision_id || null,
      has_preview: Boolean(page.preview_revision_id),
      sections: (wantPreview ? page.previewSections || [] : page.sections).map(serializeSection),
      related_topics: [],
      related_topic_ids: [],
      viewing_revision_id: wantPreview ? page.preview_revision_id : page.current_revision_id,
    }
    const tamper = control.detailTamper && control.detailTamper[page.id]
    if (tamper === 'other') detail.workspace_id = 'ws-sales'
    else if (tamper === 'empty') detail.workspace_id = ''
    else if (tamper === 'null') detail.workspace_id = null
    else if (tamper === 'absent') delete detail.workspace_id
    return json(res, 200, detail)
  }

  // ---- 编译任务（8C §3，全部 require_admin；limit/offset 分页） ----
  if (path === '/api/wiki-compile/runs' && method === 'GET') {
    if (!user.is_admin) return json(res, 403, { detail: '仅管理员可查看编译任务' })
    const ws = url.searchParams.get('workspace_id') || ''
    const status = url.searchParams.get('status')
    const limit = Math.min(Number(url.searchParams.get('limit')) || 20, 500)
    const offset = Number(url.searchParams.get('offset')) || 0
    const start = Date.now()
    if (control.slowRunsMs > 0 && (!control.slowRunsWs || control.slowRunsWs === ws)) await delay(control.slowRunsMs)
    const all = Object.keys(baseRuns)
      .map(effectiveRun)
      .filter((r) => r && (!ws || r.workspace_id === ws) && (!status || r.status === status))
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
    const items = all.slice(offset, offset + limit)
    runsTimeline.push({ id: ++timelineId, start, end: Date.now(), ws, count: items.length, offset })
    return json(res, 200, { total: all.length, limit, offset, runs: items.map(serializeRun) })
  }

  m = path.match(/^\/api\/wiki-compile\/runs\/([^/]+)$/)
  if (m && method === 'GET') {
    if (!user.is_admin) return json(res, 403, { detail: '仅管理员可查看编译任务' })
    const run = effectiveRun(m[1])
    if (!run) return json(res, 404, { detail: '编译任务不存在' })
    return json(res, 200, { ...serializeRun(run), stages: stagesOf(run), artifacts: [] })
  }

  m = path.match(/^\/api\/wiki-compile\/runs\/([^/]+)\/retry$/)
  if (m && method === 'POST') {
    if (!user.is_admin) return json(res, 403, { detail: '仅管理员可执行此操作' })
    if (control.slowRetryMs > 0) await delay(control.slowRetryMs)
    if (control.retry409) return json(res, 409, { detail: '任务状态不可重试' })
    const run = effectiveRun(m[1])
    if (!run) return json(res, 404, { detail: '编译任务不存在' })
    if (run.status !== 'failed') return json(res, 409, { detail: '仅失败状态可重试' })
    const nextAttempt = (Number(run.attempt) || 1) + 1
    liveStates[m[1]] = { status: 'queued', cancel_requested: false, attempt: nextAttempt, current_stage: null, finished_at: null }
    return json(res, 200, { run_id: run.id, status: 'queued', attempt: nextAttempt, message: '已重新入队' })
  }

  m = path.match(/^\/api\/wiki-compile\/runs\/([^/]+)\/cancel$/)
  if (m && method === 'POST') {
    if (!user.is_admin) return json(res, 403, { detail: '仅管理员可执行此操作' })
    const run = effectiveRun(m[1])
    if (!run) return json(res, 404, { detail: '编译任务不存在' })
    if (run.status === 'queued') {
      liveStates[m[1]] = { status: 'cancelled', cancel_requested: false, attempt: run.attempt, current_stage: run.current_stage, finished_at: run.finished_at || '2026-09-04T09:00:20' }
      return json(res, 200, { run_id: run.id, status: 'cancelled', cancel_requested: false, message: '已取消' })
    }
    if (run.status === 'running') {
      liveStates[m[1]] = { status: 'running', cancel_requested: true, attempt: run.attempt, current_stage: run.current_stage }
      // 模拟 worker 异步收敛：running → cancel_requested → cancelled（4s 后推进，便于验收观察中间态）
      setTimeout(() => {
        if (liveStates[m[1]] && liveStates[m[1]].status === 'running') {
          liveStates[m[1]] = { status: 'cancelled', cancel_requested: false, attempt: run.attempt, current_stage: run.current_stage, finished_at: '2026-09-04T09:00:40' }
        }
      }, 4000)
      return json(res, 200, { run_id: run.id, status: 'running', cancel_requested: true, message: '已请求取消' })
    }
    return json(res, 409, { detail: '任务状态不可取消' })
  }

  return json(res, 404, { detail: `mock 未实现: ${method} ${path}` })
}

const server = http.createServer(handle)
server.listen(PORT, '127.0.0.1', () => {
  console.log(`phase8c wiki-section-evidence mock listening on ${PORT}`)
})
