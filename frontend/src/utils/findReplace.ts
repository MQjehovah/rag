export interface FindMatch {
  from: number
  to: number
}

interface TextNodeLike {
  isText?: boolean
  text?: string | null
}

interface DocLike {
  descendants: (cb: (node: TextNodeLike, pos: number) => void) => void
}

/**
 * 在文档的所有文本节点内查找 (大小写不敏感)。不假设跨节点文本连续:
 * 命中范围保证落在单个文本节点内。文本节点从 pos 开始, 字符 i 的位置为 pos + i。
 */
export function findMatchesInDoc(doc: DocLike, query: string): FindMatch[] {
  const q = (query || '').trim().toLowerCase()
  const out: FindMatch[] = []
  if (!q) return out
  doc.descendants((node, pos) => {
    if (!node.isText || !node.text) return
    const lower = node.text.toLowerCase()
    let idx = lower.indexOf(q)
    while (idx !== -1) {
      out.push({ from: pos + idx, to: pos + idx + q.length })
      idx = lower.indexOf(q, idx + q.length)
    }
  })
  return out
}
