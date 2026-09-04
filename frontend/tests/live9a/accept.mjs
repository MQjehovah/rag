// Phase 9A 真实后端浏览器验收驱动器（Agent B，无 mock：全部请求走真实后端）。
//
// 前置（由主 Agent orchestrator 准备，本脚本不启动 vite/后端）：
//   1) 后端 uvicorn 运行于 8810（.phase9a 会话临时 SQLite + 已 seed + 已触发编译）。
//   2) 前端 vite 运行于 3020，/api 代理到 http://127.0.0.1:8810
//      （VITE_DEV_PORT=3020 VITE_API_PROXY_TARGET=http://127.0.0.1:8810）。
//   3) 会话数据按 phase9a/CONTRACT.md §3 冻结：账号 phase9a-admin / -editor / -reader，
//      密码均为 Phase9a!2026；工作区 Phase9A 工程/销售知识库，wiki
//      Phase9A 系统使用说明 / 用户接口参考 / 销售流程 / 故障重试主题。
//
// 运行：node frontend/tests/live9a/accept.mjs
// 退出码：0 全过；1 断言失败/驱动异常/启动失败/空结果；3 main 外未捕获致命异常。
//
// 覆盖（真实 HTTP 登录 + 真实 DOM）：
//   L1  admin 登录：工作区选择器列表同时含 Phase9A 工程/销售知识库；
//       切销售工作区可见 Phase9A 销售流程（销售库可管理），切回工程。
//   L2  default wiki：打开 Phase9A 系统使用说明读到正文（无错误占位）。
//   L3  api wiki：Phase9A 用户接口参考结构化呈现（/v1、/v2 users 端点 +
//       参数/响应媒体/业务错误码）；若回退 Markdown 如实记 FAIL。
//   L4  api Section Evidence：/v2/audit 章节 —— reader 证据空(total=0)，
//       admin 可见 ≥1 条真实来源证据。
//   L5  admin Run 面板：ws-eng 编译 runs（定位 w-fail 对应 run，展示真实
//       stage 时间线；editor 无面板且 /api/wiki-compile/runs 被 403）。
//   L6  reader 看不到销售库（workspace 选择器/HTTP 目录/直接 URL 均不可达）。
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONT = process.env.LIVE_FRONT_URL || 'http://127.0.0.1:3020'
const BACK = process.env.LIVE_BE_URL || 'http://127.0.0.1:8810'
const CDP_PORT = Number(process.env.LIVE_CDP_PORT || 9666)
const SHOTS = process.env.LIVE_SHOTS_DIR || path.join(os.tmpdir(), 'live9a-shots')
fs.mkdirSync(SHOTS, { recursive: true })

const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const PROFILE = path.join(os.tmpdir(), 'live9a-profile')
const LOGF = path.join(os.tmpdir(), 'live9a.log')

const PASSWORD = 'Phase9a!2026'
const ACCOUNTS = { admin: 'phase9a-admin', editor: 'phase9a-editor', reader: 'phase9a-reader' }

const results = []
let driverFailed = false
let summarized = false

function summarize() {
  if (summarized) return null
  summarized = true
  const passed = results.filter((r) => r.ok).length
  const failed = results.filter((r) => !r.ok).length
  console.log('\n===== 汇总 =====')
  console.log(`passed=${passed} failed=${failed} executed=${results.length}${driverFailed ? ' driverError=true' : ''}`)
  console.log(`前端: ${FRONT}  后端: ${BACK}  截图: ${SHOTS}`)
  return { passed, failed, executed: results.length }
}
function LOG(line) {
  fs.appendFileSync(LOGF, line + '\n')
  console.log(line)
}
function record(name, ok, extra = '') {
  results.push({ name, ok, extra })
  LOG(`${ok ? 'PASS' : 'FAIL'} | ${name}${extra ? ' | ' + extra : ''}`)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---- 真实后端 HTTP（登录 / 目录 / run 关联；UI 为主，HTTP 仅作身份与关联的辅助） ----
async function beFetch(p, token, opts = {}) {
  const headers = { accept: 'application/json' }
  if (token) headers.authorization = `Bearer ${token}`
  if (opts.json !== undefined) headers['content-type'] = 'application/json'
  const res = await fetch(BACK + p, {
    method: opts.method || 'GET',
    headers,
    body: opts.json !== undefined ? JSON.stringify(opts.json) : undefined,
  })
  const text = await res.text()
  let body = null
  try { body = JSON.parse(text) } catch { /* 非 JSON（如空体） */ }
  return { status: res.status, body, text }
}
async function login(username) {
  const r = await beFetch('/api/auth/login', '', { method: 'POST', json: { username, password: PASSWORD } })
  return r.status === 200 && r.body && r.body.token ? r.body.token : null
}
async function authMe(token) {
  const r = await beFetch('/api/auth/me', token)
  return r.status === 200 ? r.body : null
}

class CDP {
  constructor(wsUrl) { this.url = wsUrl; this.id = 0; this.pending = new Map(); this.events = [] }
  async open() {
    this.ws = new WebSocket(this.url)
    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) reject(new Error(JSON.stringify(msg.error)))
        else resolve(msg.result)
      } else if (msg.method) {
        this.events.push(msg)
      }
    }
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve
      this.ws.onerror = () => reject(new Error('ws error'))
    })
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  close() { try { this.ws.close() } catch {} }
}

async function evaluate(cdp, expression) {
  const r = await cdp.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) throw new Error('evaluate error: ' + JSON.stringify(r.exceptionDetails))
  return r.result?.value
}
async function waitForText(cdp, text, timeout = 15000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try {
      if (await evaluate(cdp, `document.body ? document.body.innerText.includes(${JSON.stringify(text)}) : false`)) return true
    } catch {}
    await sleep(180)
  }
  return false
}
async function waitForCss(cdp, selector, timeout = 12000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try {
      if (await evaluate(cdp, `!!document.querySelector(${JSON.stringify(selector)})`)) return true
    } catch {}
    await sleep(150)
  }
  return false
}
async function waitCondition(cdp, fnExpr, timeout = 12000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try {
      if (await evaluate(cdp, fnExpr)) return true
    } catch {}
    await sleep(150)
  }
  return false
}
async function bodyText(cdp) {
  return evaluate(cdp, 'document.body ? document.body.innerText : ""')
}
async function countSelector(cdp, selector) {
  return evaluate(cdp, `document.querySelectorAll(${JSON.stringify(selector)}).length`)
}
async function setToken(cdp, token) {
  return evaluate(cdp, `localStorage.setItem('token', ${JSON.stringify(token)}); 'ok'`)
}
async function goto(cdp, url, waitReadyText = '', timeout = 20000) {
  await cdp.send('Page.navigate', { url })
  for (let i = 0; i < 120; i++) {
    try { if ((await evaluate(cdp, 'document.readyState')) === 'complete') break } catch {}
    await sleep(120)
  }
  await sleep(400)
  if (waitReadyText && !(await waitForText(cdp, waitReadyText, timeout))) return false
  return true
}
async function shot(cdp, name) {
  try {
    const r = await cdp.send('Page.captureScreenshot', { format: 'png' })
    const file = path.join(SHOTS, name + '.png')
    fs.writeFileSync(file, Buffer.from(r.data, 'base64'))
    return file
  } catch (e) { return 'shot-failed:' + e.message }
}

// ---- 工作区选择器（.workspace-bar .el-select；下拉项为可见工作区名） ----
async function openWorkspaceSelect(cdp) {
  return evaluate(cdp, `(() => {
    const sel = document.querySelector('.workspace-bar .el-select');
    if (!sel) return false;
    const t = sel.querySelector('.el-select__wrapper') || sel;
    t.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function readWorkspaceOptions(cdp) {
  await openWorkspaceSelect(cdp)
  for (let i = 0; i < 30; i++) {
    const items = await evaluate(cdp, `[...document.querySelectorAll('.el-select-dropdown__item')]
      .filter(el => el.offsetParent !== null).map(el => el.textContent.trim())`)
    if (Array.isArray(items) && items.length) return items
    await sleep(120)
  }
  return []
}
async function selectWorkspaceByName(cdp, name) {
  await openWorkspaceSelect(cdp)
  for (let i = 0; i < 30; i++) {
    const ok = await evaluate(cdp, `(() => {
      const items = [...document.querySelectorAll('.el-select-dropdown__item')]
        .filter(el => el.offsetParent !== null && el.textContent.trim() === ${JSON.stringify(name)});
      if (!items.length) return false;
      items[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      return true;
    })()`)
    if (ok) return true
    await sleep(120)
  }
  return false
}
async function wsCurrentName(cdp) {
  return evaluate(cdp, `(() => { const el = document.querySelector('.workspace-bar .ws-current-name'); return el ? el.textContent.trim() : ''; })()`)
}

// ---- 目录 / 详情 ----
async function clickCatalogItem(cdp, title) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.catalog-item')]
      .filter(el => el.offsetParent !== null && (el.querySelector('.catalog-title')?.textContent || '').trim() === ${JSON.stringify(title)});
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function detailTitle(cdp) {
  return evaluate(cdp, `(() => { const el = document.querySelector('.detail-title'); return el ? el.textContent.trim() : ''; })()`)
}
async function detailContentText(cdp) {
  return evaluate(cdp, `(() => { const el = document.querySelector('.detail-content'); return el ? el.innerText : ''; })()`)
}
async function apiEpSummary(cdp) {
  return evaluate(cdp, `[...document.querySelectorAll('.api-ep-toggle')].map(b => ({
    m: (b.querySelector('.api-method')?.textContent || '').trim(),
    p: (b.querySelector('.api-path')?.textContent || '').trim(),
    open: b.getAttribute('aria-expanded'),
  }))`)
}
async function apiSectionBodyText(cdp, pathNeedle) {
  return evaluate(cdp, `(() => {
    const toggles = [...document.querySelectorAll('.api-ep-toggle')]
      .filter(b => (b.querySelector('.api-path')?.textContent || '').includes(${JSON.stringify(pathNeedle)}));
    const sec = toggles[0] && toggles[0].closest('.api-endpoint-section');
    return sec ? sec.innerText : '';
  })()`)
}
async function openSectionEvidence(cdp, headingNeedle) {
  return evaluate(cdp, `(() => {
    const items = [...document.querySelectorAll('.section-item')];
    const sec = items.find(el =>
      (el.querySelector('.section-heading')?.textContent || '').includes(${JSON.stringify(headingNeedle)}));
    if (!sec) return 'no-section';
    const btn = [...sec.querySelectorAll('button, .el-button')].find(b => (b.textContent || '').trim() === '章节证据');
    if (!btn) return 'no-btn';
    btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'ok';
  })()`)
}
async function closeEvidenceDrawer(cdp) {
  return evaluate(cdp, `(() => {
    const btns = [...document.querySelectorAll('.el-drawer__close-btn')]
      .filter(b => b.offsetParent !== null);
    if (!btns.length) return false;
    btns[btns.length - 1].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function evItemCount(cdp) {
  return countSelector(cdp, '.section-evidence-drawer .sev-item')
}
async function evPagerText(cdp) {
  return evaluate(cdp, `(() => { const el = document.querySelector('.section-evidence-drawer .sev-pager-text'); return el ? el.innerText : ''; })()`)
}

// ---- Run 面板 ----
async function runRowText(cdp, runId) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `(() => { const el = document.querySelector(${JSON.stringify(sel)}); return el ? el.innerText : ''; })()`)
}
async function runRowExists(cdp, runId) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `!!document.querySelector(${JSON.stringify(sel)})`)
}
async function expandRunRow(cdp, runId) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `(() => {
    const row = document.querySelector(${JSON.stringify(sel)});
    if (!row) return 'no-row';
    const head = row.querySelector('.run-head');
    if (!head) return 'no-head';
    head.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'ok';
  })()`)
}
async function runStageCount(cdp, runId) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `(() => { const row = document.querySelector(${JSON.stringify(sel)}); return row ? row.querySelectorAll('.run-stage-row').length : -1; })()`)
}

async function waitRunRow(cdp, runId, substr, timeout = 15000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    const t = await runRowText(cdp, runId)
    if (t.includes(substr)) return true
    await sleep(180)
  }
  return false
}

// 打开某工作区并进入工程目录就绪状态
async function enterWorkspace(cdp, wsId, readyTitle) {
  await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=${wsId}`)
  if (!(await waitForCss(cdp, '.catalog-pane', 15000))) return false
  if (readyTitle) return waitForText(cdp, readyTitle, 15000)
  return true
}

async function main() {
  let chrome = null
  let cdp = null
  try {
    // ---- 登录三个真实账号（后端必已就绪） ----
    const tokens = {}
    for (const role of ['admin', 'editor', 'reader']) {
      tokens[role] = await login(ACCOUNTS[role])
      if (!tokens[role]) throw new Error(`登录失败: ${ACCOUNTS[role]}（后端 ${BACK} 未就绪或凭据不符）`)
    }
    const meAdmin = await authMe(tokens.admin)
    if (!meAdmin || !(meAdmin.is_admin || (meAdmin.groups || []).includes('__local_admin__'))) {
      throw new Error('admin /api/auth/me 非管理员，无法继续')
    }
    LOG('登录成功: admin/editor/reader（真实 Bearer token）')

    // ---- 用 admin 解析工作区 / wiki / run 的稳定标识（DOM 断言仍以界面为准） ----
    const wsResp = await beFetch('/api/wiki-workspaces', tokens.admin)
    if (wsResp.status !== 200 || !wsResp.body || !wsResp.body.workspaces) throw new Error('admin 工作区列表失败: ' + wsResp.status)
    const wsByName = {}
    for (const w of wsResp.body.workspaces) wsByName[w.name] = w.id
    const engId = wsByName['Phase9A 工程知识库']
    const salesId = wsByName['Phase9A 销售知识库']
    if (!engId || !salesId) throw new Error('工作区标识缺失: ' + JSON.stringify(Object.keys(wsByName)))

    const wikiOf = async (wsId, title) => {
      const r = await beFetch(`/api/wiki?workspace_id=${encodeURIComponent(wsId)}`, tokens.admin)
      const p = ((r.body && r.body.pages) || []).find((x) => x.title === title)
      return p ? p.id : null
    }
    const wDefId = await wikiOf(engId, 'Phase9A 系统使用说明')
    const wApiId = await wikiOf(engId, 'Phase9A 用户接口参考')
    const wFailId = await wikiOf(engId, 'Phase9A 故障重试主题')
    const wSalesId = await wikiOf(salesId, 'Phase9A 销售流程')
    if (!wDefId || !wApiId) throw new Error('工程工作区 wiki 标识缺失（编译是否完成？）')

    // w-fail 对应编译 run：按 trigger_object_id 关联（admin HTTP；DOM 断言后续用 run id）
    const runsResp = await beFetch(`/api/wiki-compile/runs?workspace_id=${encodeURIComponent(engId)}`, tokens.admin)
    const runsEng = (runsResp.body && runsResp.body.runs) || []
    let wFailRun = null
    if (wFailId) {
      wFailRun = runsEng.find((r) => r.trigger_object_id === wFailId) || null
    }

    // ---- 启动 headless Chrome ----
    chrome = spawn(CHROME, [
      '--headless=new',
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${PROFILE}`,
      '--no-first-run', '--disable-gpu', '--disable-extensions', '--no-default-browser-check',
      '--window-size=1440,900', 'about:blank',
    ], { stdio: 'ignore' })
    chrome.on('error', (e) => { throw new Error('chrome spawn error: ' + e.message) })
    let tabs = []
    for (let i = 0; i < 60; i++) {
      try {
        tabs = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
        if (tabs.some((t) => t.type === 'page')) break
      } catch {}
      await sleep(250)
    }
    const page = tabs.find((t) => t.type === 'page')
    if (!page) throw new Error('no page target')
    cdp = new CDP(page.webSocketDebuggerUrl)
    await cdp.open()
    await cdp.send('Page.enable')
    await cdp.send('Runtime.enable')
    await goto(cdp, `${FRONT}/login`)
    LOG('cdp + front origin ready')

    let ok = false
    let s = ''

    // ================= L1/L2/L3/L4a/L5：admin =================
    await setToken(cdp, tokens.admin)
    ok = await enterWorkspace(cdp, engId, 'Phase9A 系统使用说明')
    record('L0 工程工作区目录就绪（admin）', ok, `ws=${await wsCurrentName(cdp)}`)
    // L1：工作区列表同时含工程与销售库；切销售工作区可见其 wiki；再切回工程（动作必执行，条件各自判定）
    const opts = await readWorkspaceOptions(cdp)
    let g = opts.includes('Phase9A 工程知识库') && opts.includes('Phase9A 销售知识库')
    record('L1a admin 工作区列表含 工程/销售 知识库', g, `options=${JSON.stringify(opts)}`)
    const selSales = await selectWorkspaceByName(cdp, 'Phase9A 销售知识库')
    const salesShown = selSales && (await waitForText(cdp, 'Phase9A 销售流程', 15000))
    record('L1b admin 切到销售工作区可见 销售流程 wiki', salesShown, `ws=${await wsCurrentName(cdp)}`)
    const selEng = await selectWorkspaceByName(cdp, 'Phase9A 工程知识库')
    const engBack = selEng && (await waitForText(cdp, 'Phase9A 系统使用说明', 15000))
    record('L1c admin 切回工程工作区（恢复默认 wiki）', engBack, `ws=${await wsCurrentName(cdp)}`)

    // L2：default wiki 正文可读
    await clickCatalogItem(cdp, 'Phase9A 系统使用说明')
    ok = await waitCondition(cdp, `(() => { const el = document.querySelector('.detail-title'); return !!el && el.textContent.trim() === ${JSON.stringify('Phase9A 系统使用说明')}; })()`, 15000)
    await waitForCss(cdp, '.detail-content', 10000)
    const defText = await detailContentText(cdp)
    const defReadOk = defText.length > 80 && defText.includes('Phase9A') && !defText.includes('加载失败') && !defText.includes('主题页不存在')
    ok = ok && defReadOk
    await shot(cdp, 'l2-default-wiki')
    record('L2 default wiki 正文可读（无错误占位）', ok, `len=${defText.length} sample=${defText.slice(0, 60).replace(/\s+/g, ' ')}`)

    // L3：api wiki 结构化端点呈现（v1/v2 users + 参数/响应媒体/业务错误码）
    await clickCatalogItem(cdp, 'Phase9A 用户接口参考')
    ok = await waitCondition(cdp, `(() => { const el = document.querySelector('.detail-title'); return !!el && el.textContent.trim() === ${JSON.stringify('Phase9A 用户接口参考')}; })()`, 15000)
    const epPresent = await waitForCss(cdp, '.api-endpoint-section', 12000)
    const eps = await apiEpSummary(cdp)
    const epPaths = (eps || []).map((e) => e.p).join(' | ')
    const hasV1 = epPaths.includes('/v1/users')
    const hasV2 = epPaths.includes('/v2/users')
    const epCount = (eps || []).length
    const body1 = await apiSectionBodyText(cdp, '/v1/users')
    const structFieldsOk = body1.includes('参数') && body1.includes('limit') && body1.includes('application/json') && body1.includes('业务错误码') && body1.includes('401')
    ok = ok && epPresent && epCount >= 2 && hasV1 && hasV2 && structFieldsOk
    await shot(cdp, 'l3-api-wiki')
    record('L3 api wiki 结构化呈现 /v1 /v2 users（参数/媒体/错误码）', ok,
      `ep=${epCount} v1=${hasV1} v2=${hasV2} fields=${structFieldsOk} paths=${epPaths.slice(0, 120)}`)
    if (!epPresent || epCount === 0) {
      const mdFallback = (await bodyText(cdp)).includes('/v1/users')
      record('L3b api wiki 未结构化（Markdown 回退如实记录）', false,
        `mdFallback=${mdFallback} epCount=${epCount}（若正文仅 Markdown 则属于回退，按契约不得假装通过）`)
      ok = false
    }

    // L4a：admin 在 /v2/audit Section 证据抽屉看到 ≥1 条真实来源
    const adminOpen = (await openSectionEvidence(cdp, '/v2/audit')) === 'ok'
    const adminSevShown = adminOpen && (await waitForCss(cdp, '.section-evidence-drawer .sev-item', 12000))
    const adminEvCount = await evItemCount(cdp)
    const adminPager = await evPagerText(cdp)
    ok = adminOpen && adminSevShown && adminEvCount >= 1 && adminPager.includes('共')
    await shot(cdp, 'l4-admin-audit-evidence')
    record('L4a admin 在 GET /v2/audit 证据非空（真实来源可见）', ok,
      `open=${adminOpen} sevShown=${adminSevShown} items=${adminEvCount} pager=${adminPager}`)
    await closeEvidenceDrawer(cdp)
    await sleep(400)

    // L5：admin Run 面板显示 ws-eng runs（w-fail 定位），展开为真实 stage 时间线
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=${engId}`)
    await waitForCss(cdp, '.admin-runs-panel', 15000)
    const panelHasRows = await waitForCss(cdp, '.compile-run-row', 12000)
    const domRows = await countSelector(cdp, '.compile-run-row')
    let failRowOk = false
    let failExtra = `domRows=${domRows}`
    if (wFailRun) {
      const runId = wFailRun.id
      failRowOk = await runRowExists(cdp, runId)
      failExtra += ` wFailRun=${runId} httpStatus=${wFailRun.status} attempt=${wFailRun.attempt}`
      if (failRowOk) {
        const ex = await expandRunRow(cdp, runId)
        const stagesShown = ex === 'ok' && (await waitForCss(cdp, '.compile-run-row[data-run-id="' + runId + '"] .run-stage-row', 15000))
        const stages = await runStageCount(cdp, runId)
        const rowTxt = await runRowText(cdp, runId)
        const attemptTxtOk = rowTxt.includes(`attempt ${wFailRun.attempt ?? '?'}`)
        failRowOk = ex === 'ok' && stagesShown && stages >= 1 && attemptTxtOk
        failExtra += ` expand=${ex} stages=${stages} attemptTxtOk=${attemptTxtOk} row=${rowTxt.replace(/\s+/g, ' ').slice(0, 140)}`
        await shot(cdp, 'l5-admin-runs-panel')
      }
    } else {
      failExtra += ' wFailRun=NOT-FOUND(HTTP)'
    }
    ok = panelHasRows && failRowOk
    record('L5 admin Run 面板显示 ws-eng runs（w-fail run + 真实 stage 时间线）', ok, failExtra)

    // ================= L5b：editor 无 Run 面板且 run API 403 =================
    await setToken(cdp, tokens.editor)
    await enterWorkspace(cdp, engId, 'Phase9A 系统使用说明')
    const edtRunsDom = await countSelector(cdp, '.admin-runs-panel')
    const edt403 = await beFetch(`/api/wiki-compile/runs?workspace_id=${encodeURIComponent(engId)}`, tokens.editor)
    ok = edtRunsDom === 0 && edt403.status === 403
    await shot(cdp, 'l5b-editor-no-runs-panel')
    record('L5b editor 无 Run 面板且 /api/wiki-compile/runs=403', ok,
      `domPanel=${edtRunsDom} http=${edt403.status}`)

    // ================= L6/L4b：reader 看不到销售库；audit 证据为空 =================
    await setToken(cdp, tokens.reader)
    await enterWorkspace(cdp, engId, 'Phase9A 系统使用说明')
    const readerOpts = await readWorkspaceOptions(cdp)
    g = readerOpts.includes('Phase9A 工程知识库') && !readerOpts.includes('Phase9A 销售知识库')
    record('L6a reader 工作区列表无销售库', g, `options=${JSON.stringify(readerOpts)}`)
    const readerWsResp = await beFetch('/api/wiki-workspaces', tokens.reader)
    const readerWsNames = ((readerWsResp.body && readerWsResp.body.workspaces) || []).map((w) => w.name)
    g = !readerWsNames.includes('Phase9A 销售知识库') && readerWsNames.includes('Phase9A 工程知识库')
    record('L6b reader HTTP 工作区目录无销售库', g, `names=${JSON.stringify(readerWsNames)}`)
    // reader 也能读工程 default wiki 正文
    const readerDefClicked = await clickCatalogItem(cdp, 'Phase9A 系统使用说明')
    const readerDefShown = readerDefClicked && (await waitCondition(cdp, `(() => { const el = document.querySelector('.detail-title'); return !!el && el.textContent.trim() === ${JSON.stringify('Phase9A 系统使用说明')}; })()`, 15000))
    const readerDefLen = (await detailContentText(cdp)).length
    g = readerDefShown && readerDefLen > 80
    record('L6c reader 可读工程 default wiki 正文', g, `click=${readerDefClicked} shown=${readerDefShown} len=${readerDefLen}`)
    // 直接 URL 访问销售工作区 → 界面“不可访问”
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=${salesId}`)
    g = await waitForText(cdp, '不可访问', 15000)
    record('L6d reader 直接打开销售工作区显示不可访问', g, ``)
    // L4b：reader 打开 api wiki /v2/audit Section 证据 → 空
    await enterWorkspace(cdp, engId, 'Phase9A 系统使用说明')
    const rApiClicked = await clickCatalogItem(cdp, 'Phase9A 用户接口参考')
    const rApiShown = rApiClicked && (await waitCondition(cdp, `(() => { const el = document.querySelector('.detail-title'); return !!el && el.textContent.trim() === ${JSON.stringify('Phase9A 用户接口参考')}; })()`, 15000))
    await sleep(500)
    s = await openSectionEvidence(cdp, '/v2/audit')
    const readerEmpty = s === 'ok' && (await waitForText(cdp, '暂无可查看的章节证据', 12000))
    const readerEvItems = await evItemCount(cdp)
    g = rApiShown && s === 'ok' && readerEmpty && readerEvItems === 0
    await shot(cdp, 'l4b-reader-audit-empty')
    record('L4b reader 在 GET /v2/audit 证据为空（不可见来源不计入）', g,
      `open=${s} empty=${readerEmpty} items=${readerEvItems}`)

    // ================= 汇总门禁 =================
    const badCount = results.filter((r) => !r.ok).length
    record('T-ALL 上述全部通过', badCount === 0, `badBefore=${badCount}`)
  } catch (e) {
    LOG('driver error: ' + (e && e.stack ? e.stack : String(e)))
    driverFailed = true
    results.push({ name: 'DRIVER-ERROR', ok: false, extra: String(e) })
  } finally {
    try { if (cdp) await Promise.race([cdp.send('Browser.close').catch(() => {}), sleep(1200)]) } catch {}
    try { if (cdp) cdp.close() } catch {}
    try { if (chrome) chrome.kill() } catch {}
    await sleep(300)
  }
}

main()
  .then(() => {
    const s = summarize()
    const code = driverFailed || !s || s.executed === 0 || s.failed > 0 ? 1 : 0
    process.exitCode = code
    LOG(`SUMMARY passed=${s ? s.passed : 0} failed=${s ? s.failed : 0} executed=${s ? s.executed : 0} exitCode=${code}`)
    setTimeout(() => process.exit(code), 1200).unref()
  })
  .catch((e) => {
    LOG('driver fatal: ' + (e && e.stack ? e.stack : String(e)))
    driverFailed = true
    summarize()
    process.exitCode = 3
    setTimeout(() => process.exit(3), 1200).unref()
  })
