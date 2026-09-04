// Phase 8A.2 浏览器验收驱动器（可重复运行，隔离 mock，不连正式后端）。
//
// 前置：
//   1) 前端 acceptance dev server 运行在 3001：cd frontend && npm run dev:acceptance
//   2) Chrome/Edge 可用（CHROME_PATH 可覆盖；默认取本机 Chrome）
// 运行：node frontend/tests/workspace-navigation/accept.mjs
// 环境变量（可选）：FRONT_URL / MOCK_PORT / CDP_PORT / SHOTS_DIR
// 退出码：
//   0  = 所有预期场景执行且全部通过
//   1  = 存在失败断言 / 驱动异常 / 启动失败 / 未执行完预期场景 / 空结果
//
// 负向自测（故意失败，应返回非零退出码；与正式验收分开记录）：
//   NEG_ASSERT=1 node accept.mjs   # 注入失败断言
//   NEG_ERROR=1  node accept.mjs   # 注入驱动异常
//
// 验收场景（真实用户路径，禁止手工 pushState 制造历史）：
//   R1  工作区列表首次 500 → 点击“重试”→ 完整恢复 ready
//   R2  无权限 workspace URL → 用户从选择器主动切到可见工作区
//   R3  真实选择器 A→B，再浏览器后退/前进恢复（列表不混杂）
//   R4a 搜索输入同步 URL 且刷新可恢复
//   R4b 分类选择同步 URL 且刷新可恢复
//   R5  应用内路由跳转到另一主题，内容同步
//   RE  编辑入口回归（编辑框可用/取消）
//   R6  未保存编辑后退 → 取消 → 内容/URL/历史保持一致（仅一个确认框）
//   R7a A 的 Revision 延迟返回，切 B 后不得出现 A 数据
//   R7b A 的 Diff 延迟返回，切 B 后不得出现 A 数据
//   R8  保存 A 期间切到 B，A 完成后不得清空/重开/修改 B 的 UI
//   R9  快速连续导航，旧恢复流程不覆盖最新 Workspace
//   R10 320px 打开主题 + 管理员绑定面板，无整页横向溢出
//   W1  详情初次读取拒绝其他/空/null/缺失 workspace_id（无旧正文，固定提示）
//   W2  详情刷新路径拒绝错域 workspace_id（清旧正文与抽屉状态）
//   F1  阅读主题时搜索：URL 保留 pageId，目录过滤，正文保持
//   F2  编辑主题时改分类：不弹确认、不丢编辑内容、pageId 保留
//   F3  搜索+分类后刷新：pageId/Workspace/筛选均恢复一致
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONT = process.env.FRONT_URL || 'http://localhost:3001'
const MOCK_PORT = Number(process.env.MOCK_PORT || 8001)
const CDP_PORT = Number(process.env.CDP_PORT || 9333)
const SHOTS = process.env.SHOTS_DIR || path.join(os.tmpdir(), 'wiki-workspace-nav-shots')
fs.mkdirSync(SHOTS, { recursive: true })

const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const PROFILE = path.join(os.tmpdir(), 'wiki-workspace-nav-profile')
const results = []
const LOGF = path.join(os.tmpdir(), 'wiki-workspace-nav.log')
const NEG_ASSERT = process.env.NEG_ASSERT === '1'
const NEG_ERROR = process.env.NEG_ERROR === '1'
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
async function bodyText(cdp) {
  return evaluate(cdp, 'document.body ? document.body.innerText : ""')
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
async function setViewport(cdp, width, height, mobile = false) {
  await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile })
}
async function clearViewport(cdp) {
  await cdp.send('Emulation.clearDeviceMetricsOverride')
}
async function getQueryParam(cdp, key) {
  return evaluate(cdp, `new URLSearchParams(location.search).get(${JSON.stringify(key)}) || ''`)
}
async function detailTitle(cdp) {
  return evaluate(cdp, `(() => { const el = document.querySelector('.detail-title'); return el ? el.textContent.trim() : ''; })()`)
}

// ---- Element Plus 交互 ----
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
async function clickByText(cdp, text) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('button, .el-button, .cite-chip, .catalog-item, .related-item, [role=button]')]
      .filter(el => el.offsetParent !== null && el.textContent.trim() === ${JSON.stringify(text)});
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
async function clickMsgBoxButton(cdp, text) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.el-message-box .el-button')]
      .filter(el => (el.textContent || '').trim() === ${JSON.stringify(text)});
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function clickCatalogItem(cdp, title) {
  return evaluate(cdp, `(() => {
    const nodes = [...document.querySelectorAll('.catalog-item')]
      .filter(el => el.offsetParent !== null && (el.querySelector('.catalog-title')?.textContent || '').trim() === ${JSON.stringify(title)});
    if (!nodes.length) return false;
    nodes[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return true;
  })()`)
}
async function setInputValue(cdp, placeholder, value) {
  return evaluate(cdp, `(() => {
    const inp = [...document.querySelectorAll('input')].find(el => el.offsetParent !== null && (el.placeholder || '').includes(${JSON.stringify(placeholder)}));
    if (!inp) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(inp, ${JSON.stringify(value)});
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
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
async function textareaValue(cdp) {
  return evaluate(cdp, `(() => { const ta = document.querySelector('.edit-box textarea'); return ta ? ta.value : ''; })()`)
}
async function chooseCategory(cdp, label) {
  await evaluate(cdp, `(() => {
    const sel = document.querySelector('.wiki-controls .el-select');
    if (!sel) return 'no-select';
    const t = sel.querySelector('.el-select__wrapper') || sel;
    t.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    return 'clicked';
  })()`)
  for (let i = 0; i < 30; i++) {
    const ok = await evaluate(cdp, `(() => {
      const items = [...document.querySelectorAll('.el-select-dropdown__item')]
        .filter(el => el.offsetParent !== null && el.textContent.trim() === ${JSON.stringify(label)});
      if (!items.length) return false;
      items[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      return true;
    })()`)
    if (ok) return 'ok'
    await sleep(120)
  }
  return 'no-option'
}
async function countSelector(cdp, selector) {
  return evaluate(cdp, `document.querySelectorAll(${JSON.stringify(selector)}).length`)
}
async function catalogTitles(cdp) {
  return evaluate(cdp, `[...document.querySelectorAll('.catalog-item .catalog-title')].map(el => el.textContent.trim())`)
}

async function mockControl(patch) {
  await fetch(`http://127.0.0.1:${MOCK_PORT}/__control`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(patch) })
}
async function resetAllControl() {
  await mockControl({
    errorWorkspaces: false, slowWorkspace: '', slowRevisionsPage: '', slowDiffPage: '', slowPatchPage: '',
    detailTamper: {},
  })
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

    // ===== 负向自测：可控注入失败/驱动异常（不执行正式场景，预期非零退出） =====
    if (NEG_ASSERT || NEG_ERROR) {
      LOG(`[SELF-TEST] negative mode: NEG_ASSERT=${NEG_ASSERT} NEG_ERROR=${NEG_ERROR}`)
      if (NEG_ASSERT) {
        record('SELF-TEST-NEG-ASSERT', false, '注入的失败断言（负向自测，预期非零退出）')
      }
      if (NEG_ERROR) {
        throw new Error('SELF-TEST-INJECTED driver error（负向自测，预期非零退出）')
      }
      return
    }

    let ok = false
    let s

    // ===== R1：工作区列表首次 500 → 重试 → 完整恢复 =====
    await resetAllControl()
    await setToken(cdp, 'tok-eng')
    await mockControl({ errorWorkspaces: true })
    await goto(cdp, `${FRONT}/knowledge/wiki`, '工作区加载失败')
    const r1err = await bodyText(cdp)
    ok = r1err.includes('工作区加载失败') && !r1err.includes('暂无可访问的 Wiki 工作区')
    record('R1a 工作区首次 500 显示可重试错误（区别于空列表）', ok)
    await mockControl({ errorWorkspaces: false })
    ok = await clickButtonByText(cdp, '重试')
    await waitForText(cdp, '水箱安装手册', 10000)
    await sleep(300)
    const r1url = await currentUrl(cdp)
    const r1body = await bodyText(cdp)
    ok = ok && r1url.includes('workspace_id=ws-eng') && r1body.includes('水箱安装手册') && !r1body.includes('正在加载工作区')
    record('R1b 点击重试后完整恢复 ready（含默认工作区写入 URL）', ok, r1url)
    await shot(cdp, 'r1-retry-recovered')

    // ===== R2：无权限 URL → 用户主动从选择器切换到可见工作区 =====
    await resetAllControl()
    await setToken(cdp, 'tok-eng')
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=ws-sales`, '当前工作区不可访问')
    await sleep(300)
    ok = !(await bodyText(cdp)).includes('水箱安装手册')
    record('R2a 无权限 URL 显示不可访问且不展示旧内容', ok)
    s = await chooseWorkspace(cdp, '工程工作区')
    await waitForText(cdp, '水箱安装手册', 10000)
    ok = s === 'ok' && (await currentUrl(cdp)).includes('workspace_id=ws-eng')
    record('R2b 用户从选择器主动切到可见工作区', ok, `choose=${s}`)
    await shot(cdp, 'r2-active-select')

    // ===== R3：真实选择器 A→B + 后退/前进恢复（列表不混杂） =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=ws-eng`, '水箱安装手册')
    s = await chooseWorkspace(cdp, '销售工作区')
    await waitForText(cdp, '报价方案模板', 10000)
    let t = await bodyText(cdp)
    ok = s === 'ok' && t.includes('报价方案模板') && !t.includes('水箱安装手册')
    record('R3a 选择器 A→B 切换（真实 push 历史）', ok, `choose=${s}`)
    await shot(cdp, 'r3-sales')
    await evaluate(cdp, 'history.back(); "ok"')
    await waitForText(cdp, '水箱安装手册', 10000)
    await sleep(300)
    t = await bodyText(cdp)
    ok = (await currentUrl(cdp)).includes('workspace_id=ws-eng') && t.includes('水箱安装手册') && !t.includes('报价方案模板')
    record('R3b 浏览器后退恢复到 A（列表不混杂）', ok)
    await shot(cdp, 'r3-back-eng')
    await evaluate(cdp, 'history.forward(); "ok"')
    await waitForText(cdp, '报价方案模板', 10000)
    ok = (await currentUrl(cdp)).includes('workspace_id=ws-sales') && (await bodyText(cdp)).includes('报价方案模板')
    record('R3c 浏览器前进恢复 B', ok)

    // ===== R4a：搜索同步 URL 且刷新可恢复 =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=ws-eng`, '水箱安装手册')
    ok = await setInputValue(cdp, '搜索', '接线')
    await waitForText(cdp, '接线规范', 8000)
    await sleep(800)
    const qVal = await getQueryParam(cdp, 'q')
    t = await bodyText(cdp)
    ok = ok && qVal === '接线' && t.includes('接线规范') && !t.includes('水箱安装手册')
    record('R4a-1 搜索输入同步到 URL 并过滤当前工作区', ok, `q=${qVal}`)
    await shot(cdp, 'r4-search')
    await goto(cdp, `${await currentUrl(cdp)}`, '接线规范')
    t = await bodyText(cdp)
    ok = (await getQueryParam(cdp, 'q')) === '接线' && t.includes('接线规范') && !t.includes('水箱安装手册')
    record('R4a-2 刷新后筛选与 URL 一致', ok)

    // ===== R4b：分类同步 URL 且刷新可恢复 =====
    ok = await setInputValue(cdp, '搜索', '')
    await sleep(800)
    await waitForText(cdp, '水箱安装手册', 8000)
    s = await chooseCategory(cdp, '安装')
    await waitForText(cdp, '水箱安装手册', 8000)
    await sleep(500)
    const catVal = await getQueryParam(cdp, 'category')
    t = await bodyText(cdp)
    ok = s === 'ok' && catVal === '安装' && t.includes('水箱安装手册') && !t.includes('接线规范')
    record('R4b-1 分类选择同步 URL 并过滤', ok, `category=${catVal}`)
    await goto(cdp, `${await currentUrl(cdp)}`, '水箱安装手册')
    t = await bodyText(cdp)
    ok = (await getQueryParam(cdp, 'category')) === '安装' && t.includes('水箱安装手册') && !t.includes('接线规范')
    record('R4b-2 刷新后分类与 URL 一致', ok)

    // ===== R5：应用内路由跳转到另一主题，内容同步 =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=ws-eng`, '水箱安装手册')
    ok = await clickCatalogItem(cdp, '水箱安装手册')
    await waitForCss(cdp, '.detail-title', 8000)
    await waitForText(cdp, '操作步骤', 8000)
    ok = ok && (await detailTitle(cdp)) === '水箱安装手册'
    record('R5a 打开主题 A 详情', ok)
    ok = await clickCatalogItem(cdp, '接线规范')
    await waitForText(cdp, '规则与约束', 8000)
    await sleep(300)
    ok = ok && (await detailTitle(cdp)) === '接线规范' && (await currentUrl(cdp)).includes('p-eng-2')
    record('R5b 应用内路由跳转到主题 B 内容同步', ok)
    await shot(cdp, 'r5-inapp-topic')

    // ===== RE：编辑入口回归 =====
    ok = await clickByText(cdp, '编辑')
    ok = ok && (await waitForCss(cdp, '.edit-box textarea', 4000))
    record('RE 编辑入口回归（编辑框打开）', ok, `click=${ok}`)
    await clickButtonByText(cdp, '取消')
    await sleep(300)

    // ===== R6：未保存编辑后退 → 取消 → 一致（仅一个确认框） =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=ws-eng`, '水箱安装手册')
    await clickCatalogItem(cdp, '水箱安装手册')
    await waitForText(cdp, '操作步骤', 8000)
    await clickByText(cdp, '编辑')
    await waitForCss(cdp, '.edit-box textarea', 4000)
    const typed = await setTextareaValue(cdp, '【未保存的手工修改-验收】')
    await sleep(300)
    await evaluate(cdp, 'history.back(); "ok"')
    const dlg = await waitForText(cdp, '未保存编辑', 6000)
    const boxes = await countSelector(cdp, '.el-message-box')
    ok = typed && dlg && boxes === 1
    record('R6a 未保存编辑后退触发且仅一个确认框', ok, `typed=${typed} boxes=${boxes}`)
    await shot(cdp, 'r6-confirm-back')
    ok = await clickMsgBoxButton(cdp, '取消')
    await waitForTextGone(cdp, '未保存编辑', 5000)
    const url6 = await currentUrl(cdp)
    const val6 = await textareaValue(cdp)
    ok = ok && url6.includes('p-eng-1') && url6.includes('workspace_id=ws-eng') && (await detailTitle(cdp)) === '水箱安装手册' && val6.includes('未保存的手工修改')
    record('R6b 取消后内容/URL/编辑一致', ok, url6)
    await shot(cdp, 'r6-cancel-consistent')

    // ===== R7a：A 的 Revision 延迟返回，切 B 后不得出现 A 数据 =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await mockControl({ slowRevisionsPage: 'p-eng-1' })
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    ok = await clickButtonByText(cdp, '版本历史')
    await sleep(400)
    ok = ok && (await clickCatalogItem(cdp, '接线规范'))
    await waitForText(cdp, '规则与约束', 8000)
    await sleep(2300)
    const revItems = await countSelector(cdp, '.rev-item')
    const revDrawerItems = await countSelector(cdp, '.el-drawer .rev-item')
    t = await bodyText(cdp)
    ok = ok && (await detailTitle(cdp)) === '接线规范' && revItems === 0 && revDrawerItems === 0 && !t.includes('A补丁')
    record('R7a Revision 迟到不污染 B', ok, `revItems=${revItems}`)

    // ===== R7b：A 的 Diff 延迟返回，切 B 后不得出现 A 数据 =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await mockControl({ slowDiffPage: 'p-eng-1' })
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    ok = await clickButtonByText(cdp, '版本历史')
    ok = ok && (await waitForCss(cdp, '.rev-item', 6000))
    ok = ok && (await clickByText(cdp, '对比'))
    await sleep(300)
    ok = ok && (await clickCatalogItem(cdp, '接线规范'))
    await waitForText(cdp, '规则与约束', 8000)
    await sleep(2400)
    const diffBoxes = await countSelector(cdp, '.diff-box')
    t = await bodyText(cdp)
    ok = ok && (await detailTitle(cdp)) === '接线规范' && diffBoxes === 0 && !t.includes('A页Diff内容')
    record('R7b Diff 迟到不污染 B', ok, `diffBoxes=${diffBoxes}`)
    await shot(cdp, 'r7b-late-diff-guard')

    // ===== R8：保存 A 期间切到 B，A 完成后不得干扰 B =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await mockControl({ slowPatchPage: 'p-eng-1' })
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    await clickByText(cdp, '编辑')
    await waitForCss(cdp, '.edit-box textarea', 4000)
    await setTextareaValue(cdp, '【保存中的修改-验收】')
    await sleep(200)
    ok = await clickButtonByText(cdp, '保存')
    await sleep(350)
    ok = ok && (await clickCatalogItem(cdp, '接线规范'))
    await waitForText(cdp, '未保存编辑', 6000)
    ok = ok && (await clickMsgBoxButton(cdp, '继续切换'))
    await waitForText(cdp, '规则与约束', 8000)
    await sleep(2500)
    const t8 = await bodyText(cdp)
    const editBox8 = await countSelector(cdp, '.edit-box')
    ok = ok && (await detailTitle(cdp)) === '接线规范' && (await currentUrl(cdp)).includes('p-eng-2') && editBox8 === 0 && !t8.includes('保存中的修改-验收')
    record('R8 保存 A 期间切 B，A 完成后不干扰 B UI', ok, `editBox=${editBox8}`)
    await shot(cdp, 'r8-save-switch-guard')

    // ===== R9：快速连续导航，旧恢复流程不覆盖最新 Workspace =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await mockControl({ slowWorkspace: 'ws-eng' })
    await goto(cdp, `${FRONT}/knowledge/wiki?workspace_id=ws-sales`, '报价方案模板')
    const c1 = await chooseWorkspace(cdp, '工程工作区') // 慢(1.6s)
    await sleep(300)
    const c2 = await chooseWorkspace(cdp, '销售工作区') // 快
    await waitForText(cdp, '报价方案模板', 8000)
    await sleep(2600) // 等 ws-eng 迟到响应返回
    t = await bodyText(cdp)
    ok = c1 === 'ok' && c2 === 'ok' && (await currentUrl(cdp)).includes('workspace_id=ws-sales') && t.includes('报价方案模板') && !t.includes('水箱安装手册')
    record('R9 快速连续导航后停留在最新 Workspace（旧恢复不覆盖）', ok, `c1=${c1} c2=${c2}`)
    await shot(cdp, 'r9-rapid-nav')
    await resetAllControl()

    // ===== R10：320px 打开主题 + 管理员绑定面板，无横向溢出 =====
    await resetAllControl()
    await setToken(cdp, 'tok-admin')
    await setViewport(cdp, 320, 800, false)
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    await waitForText(cdp, '工程知识库', 8000)
    const ov = await evaluate(cdp, `(() => {
      const p = document.querySelector('.ws-notebooks');
      return {
        docW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        panelVisible: !!p && p.offsetParent !== null,
        detailTitle: (document.querySelector('.detail-title') || {}).textContent || ''
      };
    })()`)
    ok = ov.panelVisible && ov.detailTitle === '水箱安装手册' && ov.docW <= ov.innerW + 2 && ov.innerW <= 320
    record('R10 320px 主题+管理员绑定面板可用且无横向溢出', ok, JSON.stringify(ov))
    await shot(cdp, 'r10-narrow-admin-detail')
    await clearViewport(cdp)

    // ===== W1：详情初次读取严格拒绝错误 workspace_id（other/empty/null/absent） =====
    for (const mode of ['other', 'empty', 'null', 'absent']) {
      await resetAllControl()
      await setToken(cdp, 'tok-both')
      await mockControl({ detailTamper: { 'p-eng-1': mode } })
      await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '该主题不属于当前工作区')
      await sleep(250)
      const w1sections = await countSelector(cdp, '.section-item')
      const w1title = await detailTitle(cdp)
      const w1body = await bodyText(cdp)
      ok = w1sections === 0 && w1title === '' && w1body.includes('该主题不属于当前工作区')
      record(`W1-${mode} 初次读取拒绝错域详情（无旧正文/固定提示）`, ok, `sections=${w1sections}`)
    }

    // ===== W2：详情刷新路径拒绝错域 workspace_id（清旧正文与抽屉状态） =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    ok = await clickByText(cdp, '编辑')
    ok = ok && (await waitForCss(cdp, '.edit-box textarea', 4000))
    ok = ok && (await setTextareaValue(cdp, '【刷新反例-保存内容】'))
    await sleep(250)
    await mockControl({ detailTamper: { 'p-eng-1': 'other' } })
    ok = ok && (await clickButtonByText(cdp, '保存'))
    const w2msg = await waitForText(cdp, '该主题不属于当前工作区', 8000)
    await sleep(300)
    const w2url = await currentUrl(cdp)
    const w2title = await detailTitle(cdp)
    const w2sections = await countSelector(cdp, '.section-item')
    const w2edit = await countSelector(cdp, '.edit-box')
    const w2rev = await countSelector(cdp, '.rev-item')
    const w2body = await bodyText(cdp)
    ok = ok && w2msg && w2title === '' && w2sections === 0 && w2edit === 0 && w2rev === 0 && w2url.includes('p-eng-1') && !w2body.includes('刷新反例-保存内容')
    record('W2 刷新路径拒绝错域详情并清空旧正文/编辑/抽屉', ok, `sections=${w2sections} edit=${w2edit} rev=${w2rev}`)
    await shot(cdp, 'w2-refresh-mismatch')

    // ===== F1：阅读主题时搜索：URL 保留 pageId、目录过滤、正文保持 =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    ok = await setInputValue(cdp, '搜索', '接线')
    await sleep(1100)
    const f1url = await currentUrl(cdp)
    const f1q = await getQueryParam(cdp, 'q')
    const f1cats = await catalogTitles(cdp)
    const f1title = await detailTitle(cdp)
    const f1body = await bodyText(cdp)
    ok = ok && f1url.includes('/knowledge/wiki/p-eng-1') && f1url.includes('workspace_id=ws-eng') && f1q === '接线'
      && f1cats.length === 1 && f1cats[0] === '接线规范' && f1title === '水箱安装手册' && f1body.includes('操作步骤')
    record('F1 阅读主题时搜索保留 pageId/正文，目录按当前工作区过滤', ok, `q=${f1q} cats=${JSON.stringify(f1cats)}`)
    await shot(cdp, 'f1-read-search-pageid')

    // ===== F2：编辑主题时改变分类：不弹确认、不丢编辑内容、pageId 保留 =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    await clickByText(cdp, '编辑')
    await waitForCss(cdp, '.edit-box textarea', 4000)
    await setTextareaValue(cdp, '【编辑中改分类-验收】')
    await sleep(250)
    s = await chooseCategory(cdp, '电气')
    await sleep(1000)
    const f2boxes = await countSelector(cdp, '.el-message-box')
    const f2val = await textareaValue(cdp)
    const f2url = await currentUrl(cdp)
    const f2title = await detailTitle(cdp)
    const f2cat = await getQueryParam(cdp, 'category')
    const f2cats = await catalogTitles(cdp)
    ok = s === 'ok' && f2boxes === 0 && f2val.includes('编辑中改分类') && f2title === '水箱安装手册'
      && f2url.includes('/knowledge/wiki/p-eng-1') && f2cat === '电气' && f2cats.length === 1 && f2cats[0] === '接线规范'
    record('F2 编辑主题时改分类：无确认框、编辑不丢、pageId 保留', ok, `boxes=${f2boxes} cat=${f2cat}`)
    await shot(cdp, 'f2-edit-category')
    await clickButtonByText(cdp, '取消')
    await sleep(250)

    // ===== F3：搜索+分类后刷新：pageId/Workspace/筛选均恢复一致 =====
    await resetAllControl()
    await setToken(cdp, 'tok-both')
    await goto(cdp, `${FRONT}/knowledge/wiki/p-eng-1?workspace_id=ws-eng`, '操作步骤')
    await setInputValue(cdp, '搜索', '接线')
    await sleep(1000)
    s = await chooseCategory(cdp, '电气')
    await sleep(1000)
    const f3pre = await currentUrl(cdp)
    const f3preTitle = await detailTitle(cdp)
    const f3preCats = await catalogTitles(cdp)
    ok = s === 'ok' && f3pre.includes('/knowledge/wiki/p-eng-1') && f3pre.includes('workspace_id=ws-eng')
      && (await getQueryParam(cdp, 'q')) === '接线' && (await getQueryParam(cdp, 'category')) === '电气'
      && f3preTitle === '水箱安装手册' && f3preCats.length === 1 && f3preCats[0] === '接线规范'
    record('F3a 搜索+分类后 URL 含 pageId/workspace/筛选', ok, `url=${f3pre}`)
    await goto(cdp, f3pre, '操作步骤')
    await sleep(300)
    const f3title = await detailTitle(cdp)
    const f3cats = await catalogTitles(cdp)
    const f3body = await bodyText(cdp)
    ok = (await currentUrl(cdp)).includes('/knowledge/wiki/p-eng-1')
      && (await getQueryParam(cdp, 'q')) === '接线' && (await getQueryParam(cdp, 'category')) === '电气'
      && (await currentUrl(cdp)).includes('workspace_id=ws-eng') && f3title === '水箱安装手册'
      && f3cats.length === 1 && f3cats[0] === '接线规范' && f3body.includes('操作步骤')
    record('F3b 刷新后 pageId/Workspace/筛选一致（正文可继续阅读）', ok, `cats=${JSON.stringify(f3cats)}`)
    await shot(cdp, 'f3-refresh-consistent')
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

async function currentUrl(cdp) {
  return evaluate(cdp, 'location.href')
}

main()
  .then(() => {
    const s = summarize()
    const code = driverFailed || !s || s.executed === 0 || s.failed > 0 ? 1 : 0
    process.exitCode = code
    LOG(`SUMMARY passed=${s ? s.passed : 0} failed=${s ? s.failed : 0} executed=${s ? s.executed : 0} exitCode=${code}${NEG_ASSERT || NEG_ERROR ? ' [NEGATIVE-SELF-TEST]' : ''}`)
    // 资源已清理；unref 定时器仅作兜底，不阻止事件循环自然退出
    setTimeout(() => process.exit(code), 1500).unref()
  })
  .catch((e) => {
    LOG('driver fatal: ' + (e && e.stack ? e.stack : String(e)))
    driverFailed = true
    summarize()
    process.exitCode = 3
    setTimeout(() => process.exit(3), 1500).unref()
  })
