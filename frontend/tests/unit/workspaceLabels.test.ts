import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  buildWorkspaceOptionLabels,
  shortWorkspaceId,
  workspaceBaseLabel,
  type WorkspaceLabelInput,
} from '../../src/utils/workspaceLabels'

function freeze(ws: WorkspaceLabelInput): WorkspaceLabelInput {
  return Object.freeze({ ...ws })
}

test('workspaceBaseLabel prefers trimmed display_name over name', () => {
  assert.equal(
    workspaceBaseLabel({ id: 'aaaaaaaa-1111', name: 'admin', display_name: '  钉钉知识库  ' }),
    '钉钉知识库',
  )
})

test('workspaceBaseLabel falls back to name when display_name is blank', () => {
  assert.equal(
    workspaceBaseLabel({ id: 'aaaaaaaa-1111', name: ' 工程工作区 ', display_name: '   ' }),
    '工程工作区',
  )
})

test('workspaceBaseLabel falls back to id when name and display_name are empty', () => {
  assert.equal(workspaceBaseLabel({ id: 'plain-id', name: '  ', display_name: '' }), 'plain-id')
})

test('shortWorkspaceId uses first 8 chars or full id when shorter', () => {
  assert.equal(shortWorkspaceId('32520e35-aaaa-bbbb'), '32520e35')
  assert.equal(shortWorkspaceId('abc'), 'abc')
})

test('distinct display_name values are shown even when raw name is admin', () => {
  const a = freeze({ id: '11111111-aaaa', name: 'admin', display_name: '钉钉知识库' })
  const b = freeze({ id: '22222222-bbbb', name: 'admin', display_name: 'FAE 内部知识库' })
  const labels = buildWorkspaceOptionLabels([a, b])
  assert.equal(labels.get(a.id), '钉钉知识库')
  assert.equal(labels.get(b.id), 'FAE 内部知识库')
})

test('duplicate display_name appends distinct short ids', () => {
  const a = freeze({ id: '32520e35-1111-4000-8000-aaa', name: 'admin', display_name: '钉钉知识库' })
  const b = freeze({ id: '9b8c7d6e-2222-4000-8000-bbb', name: 'admin', display_name: '钉钉知识库' })
  const labels = buildWorkspaceOptionLabels([a, b])
  assert.equal(labels.get(a.id), '钉钉知识库 · 32520e35')
  assert.equal(labels.get(b.id), '钉钉知识库 · 9b8c7d6e')
})

test('unique labels do not append an id', () => {
  const only = freeze({ id: '32520e35-1111', name: '工程工作区', display_name: '工程工作区' })
  const labels = buildWorkspaceOptionLabels([only])
  assert.equal(labels.get(only.id), '工程工作区')
  assert.equal(labels.get(only.id)?.includes('32520e35'), false)
})

test('archived suffix is not part of identity grouping', () => {
  const a = freeze({ id: 'aaaaaaaa-1', name: 'admin', display_name: '共享库', status: 'archived' })
  const b = freeze({ id: 'bbbbbbbb-2', name: 'admin', display_name: '共享库', status: 'active' })
  const labels = buildWorkspaceOptionLabels([a, b])
  assert.equal(labels.get(a.id), '共享库 · aaaaaaaa（已归档）')
  assert.equal(labels.get(b.id), '共享库 · bbbbbbbb')
})

test('output order follows input order', () => {
  const items = [
    freeze({ id: 'c', name: 'C 库', display_name: 'C 库' }),
    freeze({ id: 'a', name: 'A 库', display_name: 'A 库' }),
    freeze({ id: 'b', name: 'B 库', display_name: 'B 库' }),
  ]
  assert.deepEqual([...buildWorkspaceOptionLabels(items).keys()], ['c', 'a', 'b'])
})

test('does not mutate original workspace objects', () => {
  const ws = { id: '32520e35-x', name: 'admin', display_name: '钉钉知识库', status: 'active' }
  const snapshot = JSON.stringify(ws)
  buildWorkspaceOptionLabels([ws, { ...ws, id: '9b8c7d6e-y' }])
  assert.equal(JSON.stringify(ws), snapshot)
})
