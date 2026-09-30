export type DiffType = 'equal' | 'add' | 'del'

export interface DiffOp {
  type: DiffType
  text: string
}

export interface DiffStats {
  add: number
  del: number
  equal: number
}

/** LCS 动态规划单元格上限; 超出后退化为「整块删除+整块新增」, 避免大文档卡死 UI。 */
export const MAX_LCS_CELLS = 2_000_000

/**
 * 行级 diff(以 \n 切行, 行内容精确比较):
 * equal = 两边相同; add = 仅新文本有; del = 仅旧文本有。
 * 先剥离公共前缀/后缀, 中间段做 LCS 回溯, 保证输出顺序可直接按 unified 样式渲染。
 */
export function diffLines(oldText: string, newText: string): DiffOp[] {
  const a = splitLines(oldText)
  const b = splitLines(newText)

  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start++
  let endA = a.length
  let endB = b.length
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--
    endB--
  }

  const ops: DiffOp[] = []
  for (let i = 0; i < start; i++) ops.push({ type: 'equal', text: a[i] })

  const midA = a.slice(start, endA)
  const midB = b.slice(start, endB)
  if (midA.length * midB.length > MAX_LCS_CELLS) {
    for (const text of midA) ops.push({ type: 'del', text })
    for (const text of midB) ops.push({ type: 'add', text })
  } else {
    ops.push(...lcsDiff(midA, midB))
  }

  for (let i = endA; i < a.length; i++) ops.push({ type: 'equal', text: a[i] })
  return ops
}

/** 空文本视为 0 行(而不是 1 个空行), 与常见 diff 工具语义一致。 */
function splitLines(text: string): string[] {
  const s = String(text ?? '')
  return s === '' ? [] : s.split('\n')
}

function lcsDiff(a: string[], b: string[]): DiffOp[] {  const n = a.length
  const m = b.length
  if (n === 0) return b.map(text => ({ type: 'add' as const, text }))
  if (m === 0) return a.map(text => ({ type: 'del' as const, text }))

  const dp: Int32Array[] = []
  for (let i = 0; i <= n; i++) dp.push(new Int32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }

  const ops: DiffOp[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push({ type: 'equal', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ type: 'del', text: a[i] })
      i++
    } else {
      ops.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < n) ops.push({ type: 'del', text: a[i++] })
  while (j < m) ops.push({ type: 'add', text: b[j++] })
  return ops
}

export function diffStats(ops: DiffOp[]): DiffStats {
  const stats: DiffStats = { add: 0, del: 0, equal: 0 }
  for (const op of ops) stats[op.type]++
  return stats
}
