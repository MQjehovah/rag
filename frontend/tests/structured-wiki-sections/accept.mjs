// Phase 8B 结构化 Wiki 章节浏览器验收驱动器（可重复运行，隔离 mock，不连正式后端/DB/模型）。
//
// 前置：
//   1) 前端 acceptance dev server 运行在 3001：cd frontend && npm run dev:acceptance
//   2) Chrome/Edge 可用（CHROME_PATH 可覆盖；默认取本机 Chrome）
// 运行：node frontend/tests/structured-wiki-sections/accept.mjs
// 环境变量（可选）：FRONT_URL / MOCK_PORT / CDP_PORT / SHOTS_DIR / CHROME_PATH
// 退出码：
//   0  = 所有预期场景执行且全部通过
//   1  = 失败断言 / 驱动异常 / 启动失败 / 未执行完预期场景 / 空结果
//   3  = main 之外未捕获的致命异常（兜底）
// 负向自测（故意失败，应返回非零；与正式验收分开记录）：
//   NEG_ASSERT=1 node accept.mjs    # 注入失败断言
//   NEG_ERROR=1  node accept.mjs    # 注入驱动异常
//
// 场景（真实 DOM/点击/键盘；禁止手工 pushState 制造历史）：
//   T1  打开 pageA：3 个 endpoint 结构化章节 + overview Markdown
//   T2  参数/响应/错误码表数据准确、无虚构列、HTTP 状态空=“未提供”
//   T3  v1/v2 可区分：版本标签不同、v2 有 path 参数 userId 而 v1 无
//   T4  折叠/展开（click）+ 键盘 Enter（aria-expanded）
//   T5  历史/未知结构回退 Markdown（无结构化表格元素）
//   T6  人工编辑保存后旧结构化表格消失、正文更新
//   T7  protected/manual 章节 Markdown 原文保留
//   T8  折叠状态不跨 Wiki/页面串用，返回原页仍折叠
//   T9  普通 wiki 回归：编辑 / Revision / Diff / Evidence 入口
//   T10 含 <img onerror> 的 display 字段安全（纯文本，不执行）
//   T11 1400px 与 320px 截图 + 320 无整页横向溢出、表格局部滚动
//   T12 退出码门禁回归（正常全过 exit 0）
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONT = process.env.FRONT_URL || 'http://localhost:3001'
const MOCK_PORT = Number(process.env.MOCK_PORT || 8001)
const CDP_PORT = Number(process.env.CDP_PORT || 9444)
const SHOTS = process.env.SHOTS_DIR || path.join(os.tmpdir(), 'structured-wiki-shots')
fs.mkdirSync(SHOTS, { recursive: true })

const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const PROFILE = path.join(os.tmpdir(), 'structured-wiki-profile')
const LOGF = path.join(os.tmpdir(), 'structured-wiki.log')
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
  constructor(wsUrl) { this.url = wsUrl; this.id = 0; this.pending = new Map() }
  async open() {
    this.ws = new WebSocket(this.url)
    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) reject(new Error(JSON.stringify(msg.error)))
        else resolve(msg.result)
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
async function waitForGone(cdp, text, timeout = 6000) {
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

// —— 目录项（真实应用内点击，客户端路由跳转，保留组件状态语义） ——
async function clickCatalogItem(cdp, title) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.catalog-item')]
      .filter(el => el.offsetParent !== null && (el.querySelector('.catalog-title')?.textContent || '').trim() === ${JSON.stringify(title)});
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}

// —— Element Plus 文本点击 ——
async function clickButtonByText(cdp, text) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.el-button')]
      .filter(el => el.offsetParent !== null && (el.textContent || '').trim() === ${JSON.stringify(text)});
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function clickTextEl(cdp, text) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('button, .cite-chip, .el-button, [role=button]')]
      .filter(el => el.offsetParent !== null && (el.textContent || '').trim().startsWith(${JSON.stringify(text)}));
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function setTextareaValue(cdp, value) {
  return evaluate(cdp, `(() => {
    const ta = document.querySelector('.edit-box textarea');
    if (!ta) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(ta, ${JSON.stringify(value)});
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`)
}

// —— 定位结构化 toggle（method + path），返回其所在 section 的结构化信息 ——
const findToggle = `(method, path) => {
  const els = [...document.querySelectorAll('.api-ep-toggle')];
  return els.find(el =>
    (el.querySelector('.api-method')?.textContent || '').trim() === method &&
    (el.querySelector('.api-path')?.textContent || '').trim() === path) || null;
}`
async function toggleState(cdp, method, path) {
  return evaluate(cdp, `(() => {
    const hit = (${findToggle})(${JSON.stringify(method)}, ${JSON.stringify(path)});
    if (!hit) return { found: false };
    const sec = hit.closest('.section-item');
    const tbls = sec ? [...sec.querySelectorAll('table.api-table')] : [];
    const rowsOf = (sel) => {
      const t = sec ? sec.querySelector(sel) : null;
      return t ? [...t.querySelectorAll('tbody tr')].map(tr => [...tr.querySelectorAll('td')].map(td => (td.textContent || '').trim())) : [];
    };
    return {
      found: true,
      aria: hit.getAttribute('aria-expanded'),
      ver: (hit.querySelector('.api-ver')?.textContent || '').trim(),
      method: (hit.querySelector('.api-method')?.textContent || '').trim(),
      path: (hit.querySelector('.api-path')?.textContent || '').trim(),
      btnText: hit.textContent,
      hasApi: sec ? !!sec.querySelector('.api-endpoint-section') : false,
      hasMarkdown: sec ? !!sec.querySelector('.markdown-preview') : false,
      apiTables: tbls.length,
      sectionText: sec ? sec.innerText : '',
      ths: sec ? tbls.flatMap(t => [...t.querySelectorAll('th')].map(x => (x.textContent || '').trim())) : [],
      paramsRows: rowsOf('table.api-params-table'),
      responsesRows: rowsOf('table.api-responses-table'),
      errorsRows: rowsOf('table.api-errors-table'),
      paramsText: sec ? (sec.querySelector('table.api-params-table')?.innerText || '') : '',
      responsesText: sec ? (sec.querySelector('table.api-responses-table')?.innerText || '') : '',
      errorsText: sec ? (sec.querySelector('table.api-errors-table')?.innerText || '') : '',
    };
  })()`)
}
async function actToggle(cdp, kind, method, path) {
  return evaluate(cdp, `(() => {
    const hit = (${findToggle})(${JSON.stringify(method)}, ${JSON.stringify(path)});
    if (!hit) return 'no-toggle';
    if (${JSON.stringify(kind)} === 'click') {
      hit.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    } else if (${JSON.stringify(kind)} === 'enter') {
      hit.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    } else {
      hit.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', keyCode: 32, which: 32, bubbles: true, cancelable: true }));
    }
    return 'ok';
  })()`)
}

// 按 section-heading 文本定位 section-item 并读取信息
async function sectionByHeading(cdp, heading) {
  return evaluate(cdp, `(() => {
    const items = [...document.querySelectorAll('.section-item')];
    const sec = items.find(el =>
      ((el.querySelector('.section-heading')?.textContent || '').trim().replace(/^🟡 /, '') === ${JSON.stringify(heading)}));
    if (!sec) return { found: false };
    const tbls = [...sec.querySelectorAll('table.api-table')];
    return {
      found: true,
      text: sec.innerText,
      hasMarkdown: !!sec.querySelector('.markdown-preview'),
      hasApi: !!sec.querySelector('.api-endpoint-section'),
      apiTables: tbls.length,
      apiHeadings: tbls.flatMap(t => [...t.querySelectorAll('th')].map(x => (x.textContent || '').trim())),
      markdownText: sec.querySelector('.markdown-preview') ? sec.querySelector('.markdown-preview').innerText : '',
      lockedChip: sec.innerText.includes('锁定'),
      overviewMarkdown: sec.querySelector('.markdown-preview') ? sec.querySelector('.markdown-preview').innerText : '',
    };
  })()`)
}

// 点击某 endpoint section（method+path）内的“编辑”
async function clickSectionEditByToggle(cdp, method, path) {
  return evaluate(cdp, `(() => {
    const hit = (${findToggle})(${JSON.stringify(method)}, ${JSON.stringify(path)});
    if (!hit) return 'no-toggle';
    const sec = hit.closest('.section-item');
    const btn = [...sec.querySelectorAll('button')].find(b => (b.textContent || '').trim() === '编辑');
    if (!btn) return 'no-edit';
    btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'ok';
  })()`)
}

// 点击 Evidence 引用 chip（文本以“证据：”开头）
async function clickEvidenceChip(cdp) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.cite-chip')]
      .filter(el => el.offsetParent !== null && (el.textContent || '').includes('证据：'));
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
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
    await goto(cdp, `${FRONT}/login`)
    LOG('cdp + front origin ready')

    if (NEG_ASSERT || NEG_ERROR) {
      LOG(`[SELF-TEST] negative mode: NEG_ASSERT=${NEG_ASSERT} NEG_ERROR=${NEG_ERROR}`)
      if (NEG_ASSERT) record('SELF-TEST-NEG-ASSERT', false, '注入的失败断言（负向自测，预期非零退出）')
      if (NEG_ERROR) throw new Error('SELF-TEST-INJECTED driver error（负向自测，预期非零退出）')
      return
    }

    let ok = false
    await setToken(cdp, 'tok-edit')

    const PAGE_A = `${FRONT}/knowledge/wiki/p-users?workspace_id=ws-eng`
    const openPageA = () => goto(cdp, PAGE_A, 'GET /users', 15000)

    // ===== T1：打开 pageA 详情：结构化 endpoint 章节 + overview Markdown =====
    await openPageA()
    await waitForCss(cdp, '.api-endpoint-section', 8000)
    await sleep(300)
    const t1Api = await countSelector(cdp, '.api-endpoint-section')
    const t1G = await toggleState(cdp, 'GET', '/users')
    const t1P = await toggleState(cdp, 'POST', '/users')
    const t1Gi = await toggleState(cdp, 'GET', '/users/{id}')
    const t1Ov = await sectionByHeading(cdp, '主题概览')
    ok = t1Api === 3
      && t1G.found && t1G.hasApi && t1P.found && t1P.hasApi && t1Gi.found && t1Gi.hasApi
      && t1Ov.found && t1Ov.hasMarkdown && t1Ov.markdownText.includes('本手册覆盖用户管理相关接口')
    record('T1 打开 pageA：3 个 endpoint 结构化章节 + overview Markdown 渲染', ok, `api=${t1Api} ovMarkdown=${t1Ov.hasMarkdown} ovText=${t1Ov.hasMarkdown && t1Ov.markdownText.includes('本手册覆盖用户管理相关接口')}`)
    await shot(cdp, 't1-pageA-overview')

    // ===== T2：参数/响应/错误码表数据准确，无虚构列 =====
    const t2 = await toggleState(cdp, 'GET', '/users')
    const pageRow = (t2.paramsRows || []).find((r) => r[1] === 'page')
    const paramRowPageOk = !!pageRow && pageRow[0] === 'query' && pageRow[2] === '否' && (pageRow[3] || '').length > 0
    const headersOk = t2.ths.includes('位置') && t2.ths.includes('名称') && t2.ths.includes('必填') && t2.ths.includes('说明')
    const responsesOk = t2.responsesText.includes('200') && t2.responsesText.includes('401')
    const rateRow = (t2.errorsRows || []).find((r) => r[0] === 'RATE_LIMITED')
    const errorsOk = (t2.errorsText.includes('TOKEN_EXPIRED') || (t2.errorsRows || []).some((r) => r[0] === 'TOKEN_EXPIRED'))
      && !!rateRow && rateRow[2] === '未提供'
    const noFakeCols = !t2.ths.some((h) => h.includes('默认') || h.includes('类型'))
    const reqBodyNull = t2.sectionText.includes('未提供请求体')
    ok = paramRowPageOk && headersOk && responsesOk && errorsOk && noFakeCols && reqBodyNull
      && t2.sectionText.includes('业务错误码不等于 HTTP 状态')
    record('T2a GET /users 参数表（位置/名称/必填/说明，page=query+否）与请求体“未提供”', paramRowPageOk && headersOk && reqBodyNull, `pageRow=${JSON.stringify(pageRow)} headers=${JSON.stringify(t2.ths)}`)
    record('T2b 响应 200/401 + 错误码（RATE_LIMITED HTTP 未提供 + 提示 + 无虚构列）', responsesOk && errorsOk && noFakeCols && t2.sectionText.includes('业务错误码不等于 HTTP 状态'), JSON.stringify({ responsesOk, errorsOk, noFakeCols, rateRow }))
    const t2p = await toggleState(cdp, 'POST', '/users')
    ok = t2p.found && t2p.sectionText.includes('必填：是') && t2p.sectionText.includes('application/json') && t2p.sectionText.includes('含Schema')
      && t2p.sectionText.includes('USERNAME_TAKEN')
    record('T2c POST /users 请求体必填/media_type/含Schema 正确', ok, `found=${t2p.found}`)

    // ===== T3：v1 与 v2 可区分 =====
    const t3v1 = await toggleState(cdp, 'GET', '/users')
    const t3v2 = await toggleState(cdp, 'GET', '/users/{id}')
    const userIdRow = (t3v2.paramsRows || []).find((r) => r[1] === 'userId')
    const v2HasUserIdPath = !!userIdRow && userIdRow[0] === 'path' && userIdRow[2] === '是' && (userIdRow[3] || '').length > 0
    const v1NoUserId = !(t3v1.paramsRows || []).some((r) => r[1] === 'userId')
    const versionDistinct = (t3v1.ver === 'v1' || t3v1.ver.includes('v1')) && (t3v2.ver === 'v2' || t3v2.ver.includes('v2')) && t3v1.ver !== t3v2.ver
    const pathDistinct = t3v1.path === '/users' && t3v2.path === '/users/{id}'
    ok = t3v1.found && t3v2.found && versionDistinct && pathDistinct && v2HasUserIdPath && v1NoUserId
    record('T3 v1(无 path 参数) 与 v2(userId path 参数) 可区分', ok, `v1ver=${t3v1.ver} v2ver=${t3v2.ver} v2HasUserId=${v2HasUserIdPath} v1NoUserId=${v1NoUserId} userIdRow=${JSON.stringify(userIdRow)}`)

    // ===== T4：折叠/展开 + 键盘 Enter =====
    let a = (await toggleState(cdp, 'GET', '/users')).aria
    await actToggle(cdp, 'click', 'GET', '/users')
    await sleep(150)
    const aAfterClick1 = (await toggleState(cdp, 'GET', '/users')).aria
    await actToggle(cdp, 'click', 'GET', '/users')
    await sleep(150)
    const aAfterClick2 = (await toggleState(cdp, 'GET', '/users')).aria
    const t4 = await actToggle(cdp, 'enter', 'GET', '/users')
    await sleep(200)
    const aAfterEnter = (await toggleState(cdp, 'GET', '/users')).aria
    ok = a === 'true' && aAfterClick1 === 'false' && aAfterClick2 === 'true' && t4 === 'ok' && aAfterEnter === 'false'
    record('T4 点标题按钮展开/收起 + Enter 键盘切换一次', ok, `init=${a} click1=${aAfterClick1} click2=${aAfterClick2} enter=${aAfterEnter}`)

    // ===== T5：历史/未知结构（role=null）回退 Markdown =====
    const t5 = await sectionByHeading(cdp, 'GET /legacy_users（遗留说明）')
    ok = t5.found && t5.hasMarkdown && !t5.hasApi && t5.apiTables === 0
      && t5.markdownText.includes('该接口已由 GET /users 取代') && t5.markdownText.includes('无结构化表格')
    record('T5 role=null 遗留 endpoint 回退 Markdown 原文（无结构化表格）', ok, `found=${t5.found} hasMd=${t5.hasMarkdown} hasApi=${t5.hasApi} tables=${t5.apiTables}`)

    // ===== T6：人工编辑保存后旧结构化表格消失 =====
    const NEW_POST_CONTENT = '【人工改写】创建用户接口已改为批量创建方式，详细结构待后续补齐。'
    const clickEdit = await clickSectionEditByToggle(cdp, 'POST', '/users')
    await waitForCss(cdp, '.edit-box textarea', 4000)
    const typed = await setTextareaValue(cdp, NEW_POST_CONTENT)
    await sleep(250)
    const saved = await clickButtonByText(cdp, '保存')
    const editedGone = await waitCondition(cdp, `(() => {
      const sec = [...document.querySelectorAll('.section-item')].find(el =>
        (el.querySelector('.section-heading')?.textContent || '').trim() === 'POST /users（v1）');
      if (!sec) return false;
      return !sec.querySelector('.api-endpoint-section') && sec.innerText.includes('人工改写');
    })()`, 10000)
    const t6 = await sectionByHeading(cdp, 'POST /users（v1）')
    ok = clickEdit === 'ok' && typed && saved && editedGone && t6.found && t6.hasMarkdown && !t6.hasApi && t6.apiTables === 0
      && t6.text.includes(NEW_POST_CONTENT)
    record('T6 人工编辑保存后结构消失（role/display=null）且显示新正文', ok, `clickEdit=${clickEdit} typed=${typed} saved=${saved} gone=${editedGone} apiTables=${t6.apiTables}`)
    await shot(cdp, 't6-after-manual-save')

    // ===== T7：protected/manual 内容保留 =====
    const t7 = await sectionByHeading(cdp, '人工维护说明')
    ok = t7.found && t7.hasMarkdown && !t7.hasApi && t7.lockedChip
      && t7.text.includes('由管理员人工维护，重建不会自动覆盖')
    record('T7 manual/protected 章节 Markdown 原文保留（锁定）', ok, `found=${t7.found} locked=${t7.lockedChip} api=${t7.hasApi}`)

    // ===== T8：折叠状态不跨页面串用 =====
    // GET /users 目前折叠（T4 Enter 后 false）；若为展开则先折一次，确保出发前为折叠
    let curAria = (await toggleState(cdp, 'GET', '/users')).aria
    if (curAria === 'true') {
      await actToggle(cdp, 'click', 'GET', '/users')
      await sleep(150)
      curAria = (await toggleState(cdp, 'GET', '/users')).aria
    }
    ok = curAria === 'false'
    await clickCatalogItem(cdp, '订单接口手册')
    await waitForText(cdp, '分页返回订单列表', 10000)
    const t8b = await toggleState(cdp, 'GET', '/orders')
    const ordersExpanded = t8b.found && t8b.aria === 'true'
    record('T8a 折叠 A 后打开 pageB：GET /orders 默认展开（不串用）', ok && ordersExpanded, `aBefore=${curAria} ordersAria=${t8b.aria}`)
    await clickCatalogItem(cdp, '用户接口手册')
    await waitForText(cdp, '用户详情', 10000)
    await sleep(300)
    const backA = await toggleState(cdp, 'GET', '/users')
    ok = backA.found && backA.aria === 'false'
    record('T8b 返回 pageA 后 GET /users 仍为折叠', ok, `aria=${backA.aria}`)

    // ===== T9：普通 wiki 回归（编辑 / Revision / Diff / Evidence） =====
    await clickCatalogItem(cdp, '设备安装指南')
    await waitForText(cdp, '使用扭矩扳手', 10000)
    const clickEdit2 = await clickTextEl(cdp, '编辑')
    const editBoxOpen = await waitForCss(cdp, '.edit-box textarea', 4000)
    if (editBoxOpen) await clickButtonByText(cdp, '取消')
    await sleep(200)
    record('T9a 普通 wiki 编辑入口可用', clickEdit2 && editBoxOpen, `edit=${clickEdit2} box=${editBoxOpen}`)
    const revClick = await clickButtonByText(cdp, '版本历史')
    const revItems = await waitForCss(cdp, '.rev-item', 6000)
    record('T9b 版本历史 Revision 入口可用', revClick && revItems, `click=${revClick} items=${revItems}`)
    await sleep(200)
    const diffClick = await clickTextEl(cdp, '对比')
    const diffShown = await waitForText(cdp, '新增扭矩扳手要求', 8000)
    record('T9c Diff 展示可用', diffClick && diffShown, `click=${diffClick} shown=${diffShown}`)
    const evClick = await clickEvidenceChip(cdp)
    const evShown = await waitForText(cdp, '禁止使用活动扳手', 8000)
    ok = evClick && evShown
    record('T9d Evidence 证据抽屉可用', ok, `click=${evClick} shown=${evShown}`)
    await shot(cdp, 't9-plain-regression')

    // ===== T10：XSS 安全 =====
    await openPageA()
    await waitForCss(cdp, '.api-endpoint-section', 8000)
    const t10 = await toggleState(cdp, 'GET', '/users/{id}')
    const sec10 = await evaluate(cdp, `(() => {
      const hit = (${findToggle})('GET', '/users/{id}');
      const sec = hit ? hit.closest('.section-item') : null;
      const hasXssEl = !!document.querySelector('#xss-leak');
      return {
        hasXssEl,
        xssVar: typeof window.__xss,
        imgCountInDoc: document.querySelectorAll('img').length,
        descText: sec ? (sec.querySelector('.api-desc') ? sec.querySelector('.api-desc').textContent : '') : '',
      };
    })()`)
    ok = t10.found && !sec10.hasXssEl && sec10.xssVar === 'undefined'
      && sec10.descText.includes('<img id="xss-leak"')
    record('T10 display 含 <img onerror> 字段安全（纯文本、无元素、__xss 未定义）', ok, JSON.stringify(sec10))
    await shot(cdp, 't10-xss-safe')

    // ===== T11：1400px 与 320px 截图 + 无整页横向溢出 =====
    await setViewport(cdp, 1400, 900, false)
    await goto(cdp, PAGE_A, 'GET /users', 15000)
    await waitForCss(cdp, '.api-endpoint-section', 8000)
    await sleep(400)
    const deskW = await evaluate(cdp, 'window.innerWidth')
    const shotDesk = await shot(cdp, 't11-desktop-1400')
    await setViewport(cdp, 320, 800, false)
    await goto(cdp, PAGE_A, 'GET /users', 15000)
    await waitForCss(cdp, '.api-endpoint-section', 8000)
    await sleep(500)
    const narrow = await evaluate(cdp, `(() => {
      const scrollWraps = [...document.querySelectorAll('.api-table-scroll')];
      const tbls = [...document.querySelectorAll('table.api-table')];
      return {
        docScrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        apiSections: document.querySelectorAll('.api-endpoint-section').length,
        scrollWraps: scrollWraps.length,
        tables: tbls.length,
      };
    })()`)
    const shotNarrow = await shot(cdp, 't11-narrow-320')
    const noOverflow = narrow.docScrollW <= narrow.innerW + 2
    ok = deskW === 1400 && narrow.apiSections > 0 && narrow.scrollWraps > 0 && narrow.tables > 0 && noOverflow
    record('T11 桌面(1400) 与 320px 截图 + 320 表格局部滚动无整页横向溢出', ok, `desk=${deskW} narrow=${JSON.stringify(narrow)}`)
    LOG('T11 截图路径（桌面）: ' + shotDesk)
    LOG('T11 截图路径（320px）: ' + shotNarrow)
    await clearViewport(cdp)

    // ===== T12：退出码门禁回归 =====
    const badCount = results.filter((r) => !r.ok).length
    record('T12 门禁回归：上述全部通过', badCount === 0, `badBefore=${badCount}`)
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
