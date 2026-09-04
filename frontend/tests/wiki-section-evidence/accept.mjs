// Phase 8C 章节 Evidence / 编辑者诊断 / 管理员编译任务面板浏览器验收驱动器。
//
// 前置：
//   1) 前端 acceptance dev server 运行在 3001：cd frontend && npm run dev:acceptance
//   2) Chrome/Edge 可用（CHROME_PATH 可覆盖）
// 运行：node frontend/tests/wiki-section-evidence/accept.mjs
// 退出码：0 全过；1 失败断言/驱动异常/启动失败/空结果；3 main 外未捕获致命异常。
// 负向自测：NEG_ASSERT=1 / NEG_ERROR=1（预期非零）。
//
// 场景（真实 DOM/点击，禁止手工 pushState）：
//   T1  两个不同 Section 的 evidence 不串用
//   T2  空列表与失败分开（失败可注入 500）；stale/rejected/hash changed/截断提示齐全
//   T3  抽屉请求迟到/跨页面切换不覆盖（mock 延迟 + 切页后断言旧内容不出现）
//   T4  读者只打开 evidence 抽屉：请求日志无 diagnostics/compile runs
//   T5  编辑者（非 admin）可见 EditorDiagnostics 摘要；无 Run 面板、不调用 run API
//   T6  管理员按当前工作区看到 runs，按 workspace 过滤正确（含 stage 时间线）
//   T7  retry 409 受控提示并刷新；retry 成功；queued→cancelled / running→cancel_requested→cancelled
//   T8  关闭面板停止轮询；轮询请求不重叠
//   T9  结构化正文 / 既有编辑入口回归
//   T10 桌面与 320px 抽屉 / 任务面板截图（保存文件名并 LOG）
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONT = process.env.FRONT_URL || 'http://localhost:3001'
const MOCK_PORT = Number(process.env.MOCK_PORT || 8001)
const CDP_PORT = Number(process.env.CDP_PORT || 9555)
const SHOTS = process.env.SHOTS_DIR || path.join(os.tmpdir(), 'wiki-section-evidence-shots')
fs.mkdirSync(SHOTS, { recursive: true })

const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const PROFILE = path.join(os.tmpdir(), 'wiki-section-evidence-profile')
const LOGF = path.join(os.tmpdir(), 'wiki-section-evidence.log')
const NEG_ASSERT = process.env.NEG_ASSERT === '1'
const NEG_ERROR = process.env.NEG_ERROR === '1'
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
  console.log(`截图目录: ${SHOTS}`)
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
      this.ws.onerror = (e) => reject(new Error('ws error'))
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
async function waitForText(cdp, text, timeout = 12000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try {
      if (await evaluate(cdp, `document.body ? document.body.innerText.includes(${JSON.stringify(text)}) : false`)) return true
    } catch {}
    await sleep(180)
  }
  return false
}
async function waitForTextGone(cdp, text, timeout = 6000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try {
      if (!(await evaluate(cdp, `document.body ? document.body.innerText.includes(${JSON.stringify(text)}) : true`))) return true
    } catch {}
    await sleep(150)
  }
  return false
}
async function waitForCss(cdp, selector, timeout = 8000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try {
      if (await evaluate(cdp, `!!document.querySelector(${JSON.stringify(selector)})`)) return true
    } catch {}
    await sleep(150)
  }
  return false
}
async function waitCondition(cdp, fnExpr, timeout = 10000) {
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
async function currentUrl(cdp) {
  return evaluate(cdp, 'location.href')
}
async function countSelector(cdp, selector) {
  return evaluate(cdp, `document.querySelectorAll(${JSON.stringify(selector)}).length`)
}
async function setToken(cdp, token) {
  return evaluate(cdp, `localStorage.setItem('token', ${JSON.stringify(token)}); 'ok'`)
}
async function goto(cdp, url, waitReadyText = '', timeout = 15000) {
  await cdp.send('Page.navigate', { url })
  for (let i = 0; i < 100; i++) {
    try { if ((await evaluate(cdp, 'document.readyState')) === 'complete') break } catch {}
    await sleep(120)
  }
  await sleep(500)
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
async function setViewport(cdp, width, height, mobile = false) {
  await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile })
}
async function clearViewport(cdp) {
  await cdp.send('Emulation.clearDeviceMetricsOverride')
}
async function detailTitle(cdp) {
  return evaluate(cdp, `(() => { const el = document.querySelector('.detail-title'); return el ? el.textContent.trim() : ''; })()`)
}

// ---- Element Plus / 应用内点击 ----
async function clickCatalogItem(cdp, title) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.catalog-item')]
      .filter(el => el.offsetParent !== null && (el.querySelector('.catalog-title')?.textContent || '').trim() === ${JSON.stringify(title)});
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function clickButtonByText(cdp, text) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.el-button')]
      .filter(el => el.offsetParent !== null && (el.textContent || '').trim() === ${JSON.stringify(text)});
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function chooseWorkspace(cdp, wsName) {
  await evaluate(cdp, `(() => {
    const sel = document.querySelector('.workspace-bar .el-select');
    if (!sel) return 'no-select';
    const t = sel.querySelector('.el-select__wrapper') || sel;
    t.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'clicked';
  })()`)
  for (let i = 0; i < 30; i++) {
    const ok = await evaluate(cdp, `(() => {
      const items = [...document.querySelectorAll('.el-select-dropdown__item')]
        .filter(el => el.offsetParent !== null && el.textContent.trim() === ${JSON.stringify(wsName)});
      if (!items.length) return false;
      items[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      return true;
    })()`)
    if (ok) return 'ok'
    await sleep(120)
  }
  return 'no-option'
}

// 按章节标题打开对应 section-item 里的“章节证据”按钮
async function openSectionEvidence(cdp, heading) {
  return evaluate(cdp, `(() => {
    const items = [...document.querySelectorAll('.section-item')];
    const sec = items.find(el =>
      ((el.querySelector('.section-heading')?.textContent || '').trim().replace(/^🟡 /, '') === ${JSON.stringify(heading)}));
    if (!sec) return 'no-section';
    const btn = [...sec.querySelectorAll('button, .el-button')].find(b => (b.textContent || '').trim() === '章节证据');
    if (!btn) return 'no-btn';
    btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'ok';
  })()`)
}
async function closeDrawer(cdp) {
  return evaluate(cdp, `(() => {
    const btns = [...document.querySelectorAll('.section-evidence-drawer .el-drawer__close-btn, .section-evidence-drawer .el-drawer__header button')];
    if (!btns.length) return false;
    btns[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function clickDrawerRetry(cdp) {
  return evaluate(cdp, `(() => {
    const b = document.querySelector('.section-evidence-drawer .sev-retry');
    if (!b) return false;
    b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function panelText(cdp) {
  return evaluate(cdp, `(() => { const el = document.querySelector('.editor-diagnostics'); return el ? el.innerText : ''; })()`)
}
async function runRowText(cdp, runId) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `(() => { const el = document.querySelector(${JSON.stringify(sel)}); return el ? el.innerText : ''; })()`)
}
async function runRowExists(cdp, runId) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `!!document.querySelector(${JSON.stringify(sel)})`)
}
async function clickRunBtn(cdp, runId, cls) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `(() => {
    const row = document.querySelector(${JSON.stringify(sel)});
    if (!row) return 'no-row';
    const b = row.querySelector(${JSON.stringify(cls)});
    if (!b) return 'no-btn';
    b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'ok';
  })()`)
}
async function doubleClickRunBtn(cdp, runId, cls) {
  const sel = '.compile-run-row[data-run-id=' + JSON.stringify(runId) + ']'
  return evaluate(cdp, `(() => {
    const row = document.querySelector(${JSON.stringify(sel)});
    if (!row) return 'no-row';
    const b = row.querySelector(${JSON.stringify(cls)});
    if (!b) return 'no-btn';
    b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'ok';
  })()`)
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
async function clickPanelToggle(cdp) {
  return evaluate(cdp, `(() => {
    const b = document.querySelector('.admin-runs-panel .arp-toggle');
    if (!b) return false;
    b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function clickPanelRefresh(cdp) {
  return evaluate(cdp, `(() => {
    const b = document.querySelector('.admin-runs-panel .arp-refresh');
    if (!b) return false;
    b.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}

// ---- mock 控制 ----
async function mockControl(patch) {
  await fetch(`http://127.0.0.1:${MOCK_PORT}/__control`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(patch) })
}
async function resetControl() {
  await mockControl({ errorWorkspaces: false, detailTamper: {}, slowEvidenceSection: '', errorEvidenceSection: '', slowRunsMs: 0, retry409: false, slowRetryMs: 0 })
}
async function requestLog() {
  const r = await fetch(`http://127.0.0.1:${MOCK_PORT}/__log`)
  return (await r.json()).log
}
async function resetLog() {
  await fetch(`http://127.0.0.1:${MOCK_PORT}/__reset-log`, { method: 'POST' })
}
async function runsTimeline() {
  const r = await fetch(`http://127.0.0.1:${MOCK_PORT}/__runs-timeline`)
  return (await r.json()).entries
}
async function resetRunsTimeline() {
  await fetch(`http://127.0.0.1:${MOCK_PORT}/__reset-runs-timeline`, { method: 'POST' })
}

// ---- 截图 ----
async function shotElement(cdp, selector, name) {
  const rect = await evaluate(cdp, `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const r = el.getBoundingClientRect();
    return { x: Math.max(0, r.left), y: Math.max(0, r.top), width: r.width, height: r.height, vw: window.innerWidth, vh: window.innerHeight };
  })()`)
  if (!rect) return 'no-element:' + selector
  const clip = {
    x: rect.x, y: rect.y,
    width: Math.min(rect.width, rect.vw - rect.x),
    height: Math.min(rect.height, rect.vh - rect.y),
    scale: 1,
  }
  if (clip.width <= 0 || clip.height <= 0) return 'empty-clip:' + selector
  const r = await cdp.send('Page.captureScreenshot', { format: 'png', clip, captureBeyondViewport: false })
  const file = path.join(SHOTS, name + '.png')
  fs.writeFileSync(file, Buffer.from(r.data, 'base64'))
  return file
}

async function main() {
  let mockChild = null
  let chrome = null
  let cdp = null
  try {
    mockChild = spawn(process.execPath, [path.join(__dirname, 'mock-server.mjs')], { env: { ...process.env, MOCK_PORT: String(MOCK_PORT) }, stdio: 'ignore' })
    await sleep(700)
    LOG('mock spawned')
    chrome = spawn(CHROME, [
      '--headless=new',
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${PROFILE}`,
      '--no-first-run', '--disable-gpu', '--disable-extensions', '--no-default-browser-check',
      '--window-size=1400,900', 'about:blank',
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
    await cdp.send('Network.enable')
    await goto(cdp, `${FRONT}/login`)
    LOG('cdp + front origin ready')

    if (NEG_ASSERT || NEG_ERROR) {
      LOG(`[SELF-TEST] negative mode: NEG_ASSERT=${NEG_ASSERT} NEG_ERROR=${NEG_ERROR}`)
      if (NEG_ASSERT) record('SELF-TEST-NEG-ASSERT', false, '注入的失败断言（负向自测，预期非零退出）')
      if (NEG_ERROR) throw new Error('SELF-TEST-INJECTED driver error（负向自测，预期非零退出）')
      return
    }

    let ok = false
    let s = ''
    const P_MAIN = `${FRONT}/knowledge/wiki/p-main?workspace_id=ws-eng`
    const WS_ROOT = `${FRONT}/knowledge/wiki?workspace_id=ws-eng`

    // ===== T1：两个不同 Section 的 evidence 不串用 =====
    await resetControl()
    await setToken(cdp, 'tok-reader')
    await goto(cdp, P_MAIN, '第一节', 15000)
    await waitForCss(cdp, '.section-item', 8000)
    s = await openSectionEvidence(cdp, '第一节')
    ok = s === 'ok' && (await waitForText(cdp, 'EVID-A-MARK', 10000))
    await sleep(300)
    let txt = await bodyText(cdp)
    ok = ok && txt.includes('EVID-A-MARK') && !txt.includes('EVID-B-MARK')
    record('T1a 第一节证据抽屉：仅显示 A 的 Evidence', ok, `open=${s} hasA=${txt.includes('EVID-A-MARK')} hasB=${txt.includes('EVID-B-MARK')}`)
    await closeDrawer(cdp)
    await waitForTextGone(cdp, 'EVID-A-MARK', 5000)
    await sleep(500)
    s = await openSectionEvidence(cdp, '第二节')
    ok = s === 'ok' && (await waitForText(cdp, 'EVID-B-MARK', 10000))
    await sleep(300)
    txt = await bodyText(cdp)
    ok = ok && txt.includes('EVID-B-MARK') && !txt.includes('EVID-A-MARK')
    record('T1b 第二节证据抽屉：仅显示 B 的 Evidence（不串用）', ok, `open=${s} hasB=${txt.includes('EVID-B-MARK')} hasA=${txt.includes('EVID-A-MARK')}`)
    await closeDrawer(cdp)
    await waitForTextGone(cdp, 'EVID-B-MARK', 5000)
    await sleep(400)

    // ===== T2：空列表与失败分开；stale/rejected/hash changed/截断提示齐全 =====
    await resetControl()
    s = await openSectionEvidence(cdp, '状态样例')
    ok = s === 'ok'
    const t2Changed = await waitForText(cdp, '内容已变化', 10000)
    const t2Stale = await waitForText(cdp, '已过期', 10000)
    const t2Rejected = await waitForText(cdp, '已否决', 10000)
    const t2Trunc = await waitForText(cdp, '内容较长，已截断', 10000)
    record('T2a 状态样例含 hash 变化/已过期/已否决/截断提示', ok && t2Changed && t2Stale && t2Rejected && t2Trunc,
      `changed=${t2Changed} stale=${t2Stale} rejected=${t2Rejected} trunc=${t2Trunc}`)
    await closeDrawer(cdp)
    await sleep(500)
    s = await openSectionEvidence(cdp, '空样例')
    ok = s === 'ok' && (await waitForText(cdp, '暂无可查看的章节证据', 10000))
    record('T2b 空列表独立文案', ok, `open=${s}`)
    await closeDrawer(cdp)
    await sleep(500)
    await mockControl({ errorEvidenceSection: 'p-main-fail' })
    s = await openSectionEvidence(cdp, '失败样例')
    ok = s === 'ok' && (await waitForText(cdp, '章节证据加载失败', 10000)) && (await waitForText(cdp, '证据服务异常', 10000))
    record('T2c 失败与空分开（注入 500 显示独立错误）', ok, `open=${s}`)
    await mockControl({ errorEvidenceSection: '' })
    const retried = await clickDrawerRetry(cdp)
    ok = retried && (await waitForText(cdp, 'EV-FAIL-MARK', 10000))
    record('T2d 失败抽屉重试后恢复展示', ok, `retry=${retried}`)
    await closeDrawer(cdp)
    await sleep(400)

    // ===== T3：抽屉请求迟到/跨页面切换不覆盖 =====
    await resetControl()
    await mockControl({ slowEvidenceSection: 'p-main-a' })
    await goto(cdp, P_MAIN, '第一节', 15000)
    await waitForCss(cdp, '.section-item', 8000)
    s = await openSectionEvidence(cdp, '第一节')
    ok = s === 'ok'
    await sleep(300)
    ok = ok && (await clickCatalogItem(cdp, '另一手册'))
    ok = ok && (await waitForText(cdp, '章节H', 10000))
    await sleep(300)
    s = await openSectionEvidence(cdp, '章节H')
    ok = ok && s === 'ok' && (await waitForText(cdp, 'EVID-H-MARK', 10000))
    await sleep(2400)
    txt = await bodyText(cdp)
    const t3title = await detailTitle(cdp)
    ok = ok && txt.includes('EVID-H-MARK') && !txt.includes('EVID-A-MARK') && t3title === '另一手册'
    record('T3 迟到请求不覆盖跨页内容（另一手册只显示 H 的 Evidence）', ok, `title=${t3title} hasH=${txt.includes('EVID-H-MARK')} hasA=${txt.includes('EVID-A-MARK')}`)
    await closeDrawer(cdp)
    await mockControl({ slowEvidenceSection: '' })
    await sleep(400)

    // ===== T4：读者只打开 evidence 抽屉：无 diagnostics / compile runs =====
    await resetControl()
    await resetLog()
    await setToken(cdp, 'tok-reader')
    await goto(cdp, P_MAIN, '第一节', 15000)
    s = await openSectionEvidence(cdp, '第一节')
    ok = s === 'ok' && (await waitForText(cdp, 'EVID-A-MARK', 10000))
    const log4 = await requestLog()
    const diagCalls = log4.filter((e) => e.path.includes('/diagnostics'))
    const runCalls = log4.filter((e) => e.path.includes('/wiki-compile/runs'))
    const evCalls = log4.filter((e) => e.path.includes('/evidence'))
    const domDiag = await countSelector(cdp, '.editor-diagnostics')
    const domRuns = await countSelector(cdp, '.admin-runs-panel')
    ok = ok && diagCalls.length === 0 && runCalls.length === 0 && evCalls.length >= 1 && domDiag === 0 && domRuns === 0
    record('T4 读者仅请求 evidence，无 diagnostics/compile runs（DOM 亦无对应面板）', ok,
      `diag=${diagCalls.length} runs=${runCalls.length} ev=${evCalls.length} domDiag=${domDiag} domRuns=${domRuns}`)
    await closeDrawer(cdp)
    await sleep(400)

    // ===== T5：编辑者可见 EditorDiagnostics 摘要；不可见 Run 面板、不调用 run API =====
    await resetControl()
    await resetLog()
    await setToken(cdp, 'tok-editor')
    await goto(cdp, P_MAIN, '第一节', 15000)
    ok = (await waitForText(cdp, '编译诊断', 12000)) && (await waitForText(cdp, 'API Reference', 12000))
    await sleep(300)
    const p5 = await panelText(cdp)
    const log5 = await requestLog()
    const runCalls5 = log5.filter((e) => e.path.includes('/wiki-compile/runs'))
    const diagCalls5 = log5.filter((e) => e.path.includes('/api/wiki/p-main/diagnostics'))
    const domRuns5 = await countSelector(cdp, '.admin-runs-panel')
    ok = ok && p5.includes('当前 Wiki 配置') && p5.includes('自动') && p5.includes('状态样例') && p5.includes('未知')
      && diagCalls5.length >= 1 && runCalls5.length === 0 && domRuns5 === 0
    record('T5 编辑者诊断摘要可见（Skill/自动/当前配置/未知）；无 Run 面板且不调用 run API', ok,
      `diagCalls=${diagCalls5.length} runCalls=${runCalls5.length} domRuns=${domRuns5} diagTextOk=${p5.includes('当前 Wiki 配置') && p5.includes('状态样例') && p5.includes('未知')}`)

    // ===== T6：管理员按当前工作区看到 runs；按 workspace 过滤正确 =====
    await resetControl()
    await resetLog()
    await setToken(cdp, 'tok-admin')
    await goto(cdp, P_MAIN, '第一节', 15000)
    ok = (await waitForCss(cdp, '.admin-runs-panel', 8000)) && (await waitForCss(cdp, '.compile-run-row[data-run-id="cr-done"]', 8000))
    await sleep(300)
    const runRowsCount = await countSelector(cdp, '.compile-run-row')
    const t6Title = await evaluate(cdp, `(() => { const el = document.querySelector('.admin-runs-panel .arp-title'); return el ? el.textContent.trim() : ''; })()`)
    const hasEngRows = (await runRowExists(cdp, 'cr-done')) && (await runRowExists(cdp, 'cr-fail')) && (await runRowExists(cdp, 'cr-poll'))
    const hasSalesRow = await runRowExists(cdp, 'cr-sales')
    const t6Log = await requestLog()
    const engListCalls = t6Log.filter((e) => e.path === '/api/wiki-compile/runs' && e.ws === 'ws-eng')
    const crFailText = await runRowText(cdp, 'cr-fail')
    const crDoneText = await runRowText(cdp, 'cr-done')
    ok = t6Title === '当前工作区编译任务' && hasEngRows && !hasSalesRow
      && engListCalls.length >= 1 && runRowsCount === 3
      && crFailText.includes('章节编译失败') && !crDoneText.includes('章节编译失败')
    record('T6a 管理员面板标题/ws-eng 三行/无销售 run/请求带 workspace_id=ws-eng/safe error', ok,
      `title=${t6Title} rows=${runRowsCount} hasSales=${hasSalesRow} listCalls=${engListCalls.length}`)
    // stage 时间线
    s = await expandRunRow(cdp, 'cr-done')
    ok = s === 'ok' && (await waitForText(cdp, 'extract', 8000)) && (await waitForText(cdp, 'compile', 8000))
    record('T6b 展开 run 显示 stage 时间线（extract/compile）', ok, `expand=${s}`)
    // 切销售工作区：请求按 ws-sales 过滤
    s = await chooseWorkspace(cdp, '销售工作区')
    ok = s === 'ok' && (await waitForCss(cdp, '.compile-run-row[data-run-id="cr-sales"]', 10000))
    await sleep(300)
    const salesListCalls = (await requestLog()).filter((e) => e.path === '/api/wiki-compile/runs' && e.ws === 'ws-sales')
    ok = ok && salesListCalls.length >= 1 && !(await runRowExists(cdp, 'cr-done'))
    record('T6c 切到销售工作区后按 ws-sales 过滤展示', ok, `salesCalls=${salesListCalls.length}`)
    s = await chooseWorkspace(cdp, '工程工作区')
    ok = s === 'ok' && (await waitForCss(cdp, '.compile-run-row[data-run-id="cr-done"]', 10000))
    await sleep(300)
    record('T6d 切回工程工作区恢复 ws-eng runs', ok, `choose=${s}`)

    // ===== T7：retry 409 / retry 成功 / cancel 状态流 =====
    // 状态基线：cr-fail failed（默认），cr-poll succeeded（默认）
    await resetLog()
    await mockControl({ runStates: { 'cr-fail': { status: 'failed' }, 'cr-poll': { status: 'succeeded' } }, retry409: true })
    s = await clickRunBtn(cdp, 'cr-fail', '.run-retry-btn')
    ok = s === 'ok' && (await waitForText(cdp, '任务状态不可重试', 8000))
    await sleep(500)
    const stillFailed = (await runRowText(cdp, 'cr-fail')).includes('失败')
    record('T7a retry 409 受控提示并保持原状态', ok && stillFailed, `click=${s} stillFailed=${stillFailed}`)
    // 防重复：慢响应下连点两次仅一次 POST；成功后入队
    await mockControl({ retry409: false, slowRetryMs: 500, runStates: { 'cr-fail': { status: 'failed' } } })
    await resetLog()
    s = await doubleClickRunBtn(cdp, 'cr-fail', '.run-retry-btn')
    ok = s === 'ok' && (await waitCondition(cdp, `(() => { const el = document.querySelector('.compile-run-row[data-run-id="cr-fail"]'); return !!el && el.innerText.includes('排队中'); })()`, 10000))
    const retryPosts = (await requestLog()).filter((e) => e.path === '/api/wiki-compile/runs/cr-fail/retry')
    record('T7b 重试成功入队（排队中）；连点只发一个 POST', ok && retryPosts.length === 1, `posts=${retryPosts.length}`)
    // queued → cancel → 已取消
    s = await clickRunBtn(cdp, 'cr-fail', '.run-cancel-btn')
    ok = s === 'ok' && (await waitCondition(cdp, `(() => { const el = document.querySelector('.compile-run-row[data-run-id="cr-fail"]'); return !!el && el.innerText.includes('已取消'); })()`, 10000))
    record('T7c queued run 取消后显示已取消', ok, `click=${s}`)
    // running → cancel_requested → cancelled
    await mockControl({ runStates: { 'cr-poll': { status: 'running', cancel_requested: false }, 'cr-fail': { status: 'cancelled' } } })
    ok = await clickPanelRefresh(cdp)
    ok = ok && (await waitCondition(cdp, `(() => { const el = document.querySelector('.compile-run-row[data-run-id="cr-poll"]'); return !!el && el.innerText.includes('执行中'); })()`, 8000))
    ok = ok && (await waitForCss(cdp, '.compile-run-row[data-run-id="cr-poll"] .run-cancel-btn', 6000))
    s = await clickRunBtn(cdp, 'cr-poll', '.run-cancel-btn')
    ok = ok && s === 'ok'
    await waitCondition(cdp, `(() => { const el = document.querySelector('.compile-run-row[data-run-id="cr-poll"]'); return !!el && el.innerText.includes('取消请求中'); })()`, 8000)
    await sleep(400)
    const pollRowNow = await runRowText(cdp, 'cr-poll')
    ok = ok && pollRowNow.includes('取消请求中') && !pollRowNow.includes('已取消')
    record('T7d running run 提交取消后显示“取消请求中”（不假装已取消）', ok, `now=${pollRowNow.slice(0, 80)}`)
    ok = await waitCondition(cdp, `(() => { const el = document.querySelector('.compile-run-row[data-run-id="cr-poll"]'); return !!el && el.innerText.includes('已取消'); })()`, 15000)
    record('T7e 状态推进后（自动刷新）最终显示已取消', ok, ``)
    await mockControl({ slowRetryMs: 0 })

    // ===== T8：关闭面板停止轮询；轮询请求不重叠 =====
    await mockControl({ runStates: { 'cr-poll': { status: 'running', cancel_requested: false }, 'cr-fail': { status: 'cancelled' } }, slowRunsMs: 300 })
    await resetRunsTimeline()
    ok = await clickPanelRefresh(cdp)
    let entries = []
    for (let i = 0; i < 55; i++) {
      entries = await runsTimeline()
      if (entries.length >= 3) break
      await sleep(200)
    }
    ok = ok && entries.length >= 3
    let nonOverlap = true
    let gapsOk = true
    for (let i = 1; i < entries.length; i++) {
      if (entries[i].start < entries[i - 1].end) nonOverlap = false
      if (entries[i].start - entries[i - 1].end < 600) gapsOk = false
    }
    ok = ok && nonOverlap && gapsOk
    record('T8a 轮询请求不重叠且间隔合理（时间窗口检查）', ok, `windows=${entries.length} nonOverlap=${nonOverlap} gapsOk=${gapsOk}`)
    ok = await clickPanelToggle(cdp) // 收起
    await sleep(900) // 等在途请求结束
    const lenBefore = (await runsTimeline()).length
    await sleep(2600)
    const entriesAfterClose = await runsTimeline()
    ok = ok && entriesAfterClose.length === lenBefore
    record('T8b 收起面板后停止轮询（无新增窗口）', ok, `before=${lenBefore} after=${entriesAfterClose.length}`)
    await clickPanelToggle(cdp) // 展开，恢复面板
    await sleep(500)
    await mockControl({ runStates: { 'cr-poll': { status: 'succeeded' } }, slowRunsMs: 0 })
    await clickPanelRefresh(cdp)
    await sleep(1800)

    // ===== T9：结构化正文 / 既有编辑入口回归 =====
    await resetControl()
    await setToken(cdp, 'tok-editor')
    await goto(cdp, P_MAIN, '第一节', 15000)
    ok = (await waitForCss(cdp, '.api-endpoint-section', 8000)) && (await countSelector(cdp, '.api-endpoint-section')) >= 1
    const t9toggle = await evaluate(cdp, `(() => {
      const els = [...document.querySelectorAll('.api-ep-toggle')];
      const hit = els.find(el => (el.querySelector('.api-method')?.textContent || '').trim() === 'GET');
      if (!hit) return { found: false };
      return { found: true, hasPath: (hit.querySelector('.api-path')?.textContent || '').includes('/users') };
    })()`)
    ok = ok && t9toggle.found && t9toggle.hasPath
    ok = ok && (await clickButtonByText(cdp, '编辑'))
    ok = ok && (await waitForCss(cdp, '.edit-box textarea', 6000))
    ok = ok && (await clickButtonByText(cdp, '取消'))
    await sleep(300)
    const editGone = (await countSelector(cdp, '.edit-box')) === 0
    record('T9 结构化 endpoint 正文可见 + 编辑框打开/取消回归', ok && editGone, `api=ok editOpen=1 editGone=${editGone}`)

    // ===== T10：桌面与 320px 抽屉/任务面板截图 =====
    await setViewport(cdp, 1400, 900, false)
    await setToken(cdp, 'tok-reader')
    await goto(cdp, P_MAIN, '第一节', 15000)
    s = await openSectionEvidence(cdp, '状态样例')
    ok = s === 'ok' && (await waitForText(cdp, '内容较长，已截断', 10000))
    const shotDrawerDesk = await shot(cdp, 't10-drawer-desktop-1400')
    LOG('T10 截图（抽屉 桌面 1400）: ' + shotDrawerDesk)
    await closeDrawer(cdp)
    await sleep(400)
    await setViewport(cdp, 320, 800, false)
    await goto(cdp, P_MAIN, '第一节', 15000)
    s = await openSectionEvidence(cdp, '状态样例')
    ok = ok && s === 'ok' && (await waitForText(cdp, '内容较长，已截断', 10000))
    const shotDrawer320 = await shot(cdp, 't10-drawer-narrow-320')
    LOG('T10 截图（抽屉 320）: ' + shotDrawer320)
    await closeDrawer(cdp)
    await sleep(400)
    await clearViewport(cdp)

    await setViewport(cdp, 1400, 900, false)
    await setToken(cdp, 'tok-admin')
    await goto(cdp, WS_ROOT, '接口主手册', 15000)
    ok = ok && (await waitForCss(cdp, '.compile-run-row[data-run-id="cr-done"]', 8000))
    await sleep(400)
    const shotPanelDesk = await shotElement(cdp, '.admin-runs-panel', 't10-runs-panel-desktop-1400')
    LOG('T10 截图（任务面板 桌面 1400）: ' + shotPanelDesk)
    await setViewport(cdp, 320, 800, false)
    await goto(cdp, WS_ROOT, '接口主手册', 15000)
    ok = ok && (await waitForCss(cdp, '.compile-run-row[data-run-id="cr-done"]', 8000))
    await sleep(400)
    const shotPanel320 = await shotElement(cdp, '.admin-runs-panel', 't10-runs-panel-narrow-320')
    const ov320 = await evaluate(cdp, `(() => ({ docW: document.documentElement.scrollWidth, innerW: window.innerWidth }))()`)
    LOG('T10 截图（任务面板 320）: ' + shotPanel320)
    record('T10 桌面与 320px 抽屉/任务面板截图已保存', ok && ov320.docW <= ov320.innerW + 2,
      `deskDrawer=${shotDrawerDesk.split(path.sep).pop()} narrowDrawer=${shotDrawer320.split(path.sep).pop()} deskPanel=${shotPanelDesk.split(path.sep).pop()} narrowPanel=${shotPanel320.split(path.sep).pop()} overflow320=${ov320.docW}<=${ov320.innerW}`)
    await clearViewport(cdp)

    // ===== T11：门禁回归 =====
    const badCount = results.filter((r) => !r.ok).length
    record('T11 上述全部通过', badCount === 0, `badBefore=${badCount}`)
  } catch (e) {
    LOG('driver error: ' + (e && e.stack ? e.stack : String(e)))
    driverFailed = true
    results.push({ name: 'DRIVER-ERROR', ok: false, extra: String(e) })
  } finally {
    try { if (cdp) await Promise.race([cdp.send('Browser.close').catch(() => {}), sleep(1200)]) } catch {}
    try { if (cdp) cdp.close() } catch {}
    try { if (chrome) chrome.kill() } catch {}
    try { if (mockChild) mockChild.kill() } catch {}
    await sleep(300)
  }
}

main()
  .then(() => {
    const s = summarize()
    const code = driverFailed || !s || s.executed === 0 || s.failed > 0 ? 1 : 0
    process.exitCode = code
    LOG(`SUMMARY passed=${s ? s.passed : 0} failed=${s ? s.failed : 0} executed=${s ? s.executed : 0} exitCode=${code}${NEG_ASSERT || NEG_ERROR ? ' [NEGATIVE-SELF-TEST]' : ''}`)
    setTimeout(() => process.exit(code), 1500).unref()
  })
  .catch((e) => {
    LOG('driver fatal: ' + (e && e.stack ? e.stack : String(e)))
    driverFailed = true
    summarize()
    process.exitCode = 3
    setTimeout(() => process.exit(3), 1500).unref()
  })
