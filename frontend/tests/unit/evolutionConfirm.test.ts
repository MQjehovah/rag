// M3 P1 前端纯函数行为测试（node:test；无 DOM/无框架）。
// 覆盖 evolutionConfirm.ts 的 start/resume 确认构造与 rollback 幂等 pending 状态。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildRealStartConfirm,
  classifyRollbackError,
  createPendingRollback,
  newIdempotencyKey,
  retryBody,
  type PendingRollback,
  type StartPreview,
} from '../../src/utils/evolutionConfirm'

function fullPreview(): StartPreview {
  return {
    model_mode: 'real',
    dataset_version: 'wiki-default-v2dev',
    max_iterations: 2,
    budget: { max_model_calls: 260, max_tool_calls: 130, max_seconds: 1800 },
    config_fingerprint: 'cfg-fp-abc',
    reviewer_fingerprint: 'rev-fp-xyz',
  }
}

function expectReject(preview: StartPreview, label: string) {
  assert.throws(() => buildRealStartConfirm(preview), undefined, label)
}

// 1. 完整 preview 生成全部确认字段
test('buildRealStartConfirm: full preview carries every confirm field', () => {
  const c = buildRealStartConfirm(fullPreview())
  assert.deepEqual(c, {
    explicit_confirm: true,
    confirm_dataset_version: 'wiki-default-v2dev',
    confirm_max_iterations: 2,
    confirm_max_model_calls: 260,
    confirm_max_tool_calls: 130,
    confirm_max_seconds: 1800,
    confirm_config_fingerprint: 'cfg-fp-abc',
    confirm_reviewer_fingerprint: 'rev-fp-xyz',
  })
})

// 2. dataset 缺失拒绝
test('buildRealStartConfirm: rejects missing dataset_version', () => {
  const p = fullPreview()
  delete (p as { dataset_version?: string }).dataset_version
  expectReject(p, 'missing dataset')
  expectReject({ ...fullPreview(), dataset_version: '' }, 'empty dataset')
})

// 3. iterations 缺失/null/非整数/越界拒绝
test('buildRealStartConfirm: rejects iterations missing/null/non-integer/out-of-range', () => {
  const bad: Array<[StartPreview, string]> = []
  for (const iters of [undefined, null, '2', 2.5, 0, -1, NaN, Infinity]) {
    const p: StartPreview = { ...fullPreview(), max_iterations: iters as number }
    bad.push([p, 'iters=' + String(iters)])
  }
  for (const [p, label] of bad) expectReject(p, label)
})

// 4. 三项预算分别缺失/null/非正数拒绝
test('buildRealStartConfirm: rejects budget missing/null/non-positive per field', () => {
  const fields = ['max_model_calls', 'max_tool_calls', 'max_seconds'] as const
  for (const f of fields) {
    for (const v of [undefined, null, 0, -5]) {
      const good = fullPreview().budget as { max_model_calls: number; max_tool_calls: number; max_seconds: number }
      const mut: Record<string, unknown> = {
        max_model_calls: good.max_model_calls,
        max_tool_calls: good.max_tool_calls,
        max_seconds: good.max_seconds,
      }
      mut[f] = v
      const p: StartPreview = {
        ...fullPreview(),
        budget: mut as unknown as StartPreview['budget'],
      }
      expectReject(p, `${f}=${String(v)}`)
    }
  }
  // 整个 budget 缺失/null
  expectReject({ ...fullPreview(), budget: undefined }, 'budget missing')
  expectReject({ ...fullPreview(), budget: null }, 'budget null')
})

// 5. config fingerprint 缺失/null 拒绝
test('buildRealStartConfirm: rejects missing config_fingerprint', () => {
  expectReject({ ...fullPreview(), config_fingerprint: undefined }, 'cfp undefined')
  expectReject({ ...fullPreview(), config_fingerprint: null }, 'cfp null')
  expectReject({ ...fullPreview(), config_fingerprint: '' }, 'cfp empty')
})

// 6. reviewer fingerprint 有值/null 契约
test('buildRealStartConfirm: reviewer fingerprint value/null contract', () => {
  const withRev = buildRealStartConfirm(fullPreview())
  assert.equal(withRev.confirm_reviewer_fingerprint, 'rev-fp-xyz')
  // 显式 null
  const nullRev = buildRealStartConfirm({ ...fullPreview(), reviewer_fingerprint: null })
  assert.equal(nullRev.confirm_reviewer_fingerprint, null)
  // 键缺失
  const absent: StartPreview = { ...fullPreview() }
  delete (absent as { reviewer_fingerprint?: string | null }).reviewer_fingerprint
  const absentRev = buildRealStartConfirm(absent)
  assert.equal(absentRev.confirm_reviewer_fingerprint, null)
  // 空字符串非 null：如实回传（fail-closed 由服务端决定），字段必须存在
  const emptyRev = buildRealStartConfirm({ ...fullPreview(), reviewer_fingerprint: '' })
  assert.equal('confirm_reviewer_fingerprint' in emptyRev, true)
  assert.equal(emptyRev.confirm_reviewer_fingerprint, '')
})

// 7. rollback 网络错误（无响应）→ indeterminate（保留 pending）
test('classifyRollbackError: network/indeterminate keeps pending', () => {
  assert.equal(classifyRollbackError(new Error('Network Error')), 'indeterminate')
  assert.equal(classifyRollbackError({}), 'indeterminate')
  assert.equal(classifyRollbackError({ response: {} }), 'indeterminate')
  assert.equal(classifyRollbackError({ response: { status: undefined } }), 'indeterminate')
})

// 8. rollback 5xx → indeterminate（保留 pending，可同键重试）
test('classifyRollbackError: 5xx keeps pending', () => {
  for (const s of [500, 502, 503, 504]) {
    assert.equal(classifyRollbackError({ response: { status: s } }), 'indeterminate', 'status ' + s)
  }
})

// 9. rollback 409 → conflict（作废 pending）
test('classifyRollbackError: 409 invalidates pending', () => {
  assert.equal(classifyRollbackError({ response: { status: 409 } }), 'conflict')
})

// 10. rollback 422 → idem_conflict（作废 pending）
test('classifyRollbackError: 422 invalidates pending', () => {
  assert.equal(classifyRollbackError({ response: { status: 422 } }), 'idem_conflict')
})

// 11. retry body 与首次请求逐字段完全一致（且不随刷新后的新绑定变化）
test('retryBody: byte-identical reuse of original request', () => {
  const pending: PendingRollback = {
    workspace_id: 'ws-prod-1',
    expected_rev: 2,
    expected_set_hash: 'set-hash-01',
    idempotency_key: 'key-aaaa',
  }
  const first = {
    workspace_id: 'ws-prod-1',
    expected_rev: 2,
    expected_set_hash: 'set-hash-01',
    idempotency_key: 'key-aaaa',
  }
  assert.deepEqual(retryBody(pending), first)
  // pending 是不变快照：组件在发送前一次生成并复用（不因后来刷新出的新绑定改写），
  // 因此重试体与首次体逐字段一致——见 doRollback/retryRollback 共用同一 pending。
  const resend = retryBody(pending)
  assert.equal(resend.idempotency_key, first.idempotency_key)
  assert.equal(resend.workspace_id, first.workspace_id)
  assert.equal(resend.expected_rev, first.expected_rev)
  assert.equal(resend.expected_set_hash, first.expected_set_hash)
  // null 令牌按 null 原样发送（不回填 0/任意哈希）
  assert.deepEqual(
    retryBody({ workspace_id: 'w', expected_rev: null, expected_set_hash: null, idempotency_key: 'k' }),
    { workspace_id: 'w', expected_rev: null, expected_set_hash: null, idempotency_key: 'k' }
  )
})

// 12. 新操作生成新 idempotency key
test('createPendingRollback/newIdempotencyKey: fresh key per new operation', () => {
  const a = createPendingRollback('ws', 1, 'h1')
  const b = createPendingRollback('ws', 1, 'h1')
  assert.equal(a.workspace_id, 'ws')
  assert.equal(a.expected_rev, 1)
  assert.equal(a.expected_set_hash, 'h1')
  assert.notEqual(a.idempotency_key, b.idempotency_key)
  assert.notEqual(newIdempotencyKey(), newIdempotencyKey())
  // 同一 pending 反复 retry 必须同一 key（同键重试前提）
  assert.equal(a.idempotency_key, retryBody(a).idempotency_key)
})
