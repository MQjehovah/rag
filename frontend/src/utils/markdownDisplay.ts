const PAGE_HEADING_PATTERN = /^## 第\s*\d+\s*页[^\n]*$/gm
const OCR_HEADING_PATTERN = /^### 本页图片文字（OCR）\s*$/m
const OCR_NOTICE_PATTERN = /^>\s*以下内容来自页面图片自动识别，可能存在少量误差。\s*$/m
const RECOGNIZED_TABLE_HEADING_PATTERN = /^### 本页识别表格\s*$/m
const MERGED_TABLE_PATTERN = /((?:^\|[^\n]*\|\s*\n){2,})<!-- rag-table-merges: (\{[^\n]*\}) -->/gm

interface TableMergeCell {
  row: number
  col: number
  rowspan: number
  colspan: number
}

interface TableMergeMetadata {
  version: number
  cells: TableMergeCell[]
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

function splitMarkdownTableRow(line: string): string[] {
  const source = line.trim().replace(/^\||\|$/g, '')
  const cells: string[] = []
  let current = ''
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]
    if (char === '\\' && source[index + 1] === '|') {
      current += '|'
      index += 1
    } else if (char === '|') {
      cells.push(current.trim())
      current = ''
    } else {
      current += char
    }
  }
  cells.push(current.trim())
  return cells
}

function renderTableCellText(value: string): string {
  return value
    .split(/<br\s*\/?>/gi)
    .map((part) => escapeHtml(part.replace(/\\\\/g, '\\')))
    .join('<br>')
}

function validMergeCells(metadata: TableMergeMetadata, rows: string[][]): TableMergeCell[] {
  if (metadata.version !== 1 || !Array.isArray(metadata.cells)) return []
  const width = Math.max(...rows.map((row) => row.length), 0)
  return metadata.cells.filter((cell) => (
    Number.isInteger(cell.row)
    && Number.isInteger(cell.col)
    && Number.isInteger(cell.rowspan)
    && Number.isInteger(cell.colspan)
    && cell.row >= 0
    && cell.col >= 0
    && cell.rowspan >= 1
    && cell.colspan >= 1
    && cell.rowspan <= 100
    && cell.colspan <= 100
    && cell.row + cell.rowspan <= rows.length
    && cell.col + cell.colspan <= width
  ))
}

function renderMergedTable(tableSource: string, metadataSource: string): string | null {
  try {
    const tableLines = tableSource.trim().split('\n')
    if (tableLines.length < 3) return null
    const rows = tableLines
      .filter((_line, index) => index !== 1)
      .map(splitMarkdownTableRow)
    const metadata = JSON.parse(metadataSource) as TableMergeMetadata
    const merges = validMergeCells(metadata, rows)
    if (merges.length === 0) return null

    const starts = new Map<string, TableMergeCell>()
    const covered = new Set<string>()
    merges.forEach((cell) => {
      starts.set(`${cell.row}:${cell.col}`, cell)
      for (let row = cell.row; row < cell.row + cell.rowspan; row += 1) {
        for (let col = cell.col; col < cell.col + cell.colspan; col += 1) {
          if (row !== cell.row || col !== cell.col) covered.add(`${row}:${col}`)
        }
      }
    })

    const renderedRows = rows.map((row, rowIndex) => {
      const tag = rowIndex === 0 ? 'th' : 'td'
      const cells = row.map((value, colIndex) => {
        const key = `${rowIndex}:${colIndex}`
        if (covered.has(key)) return ''
        const merge = starts.get(key)
        const attributes = merge
          ? ` rowspan="${merge.rowspan}" colspan="${merge.colspan}"`
          : ''
        return `<${tag}${attributes}>${renderTableCellText(value)}</${tag}>`
      }).join('')
      return `<tr>${cells}</tr>`
    })
    return (
      '<div class="merged-table-wrapper"><table>'
      + `<thead>${renderedRows[0]}</thead>`
      + `<tbody>${renderedRows.slice(1).join('')}</tbody>`
      + '</table></div>'
    )
  } catch {
    return null
  }
}

function normalizeTableText(value: string): string {
  return value.toLowerCase().replace(/<br\s*\/?>/gi, '').replace(/[^0-9a-z\u4e00-\u9fff]+/g, '')
}

function tableWords(value: string): string[] {
  return value.toLowerCase().replace(/<br\s*\/?>/gi, ' ').match(/[0-9a-z]+|[\u4e00-\u9fff]{2,}/g) ?? []
}

function recoverFragmentedTableHeading(lines: string[]): string | null {
  for (const line of lines.slice(0, 12)) {
    if (!line.trimStart().startsWith('|')) continue
    const cells = line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim())
    const fragments = cells.map((cell) => cell.replace(/[^A-Za-z]/g, '')).filter(Boolean)
    const spacedFragments = cells.filter((cell) => /^[A-Za-z]+(?:\s+[A-Za-z]+)+$/.test(cell)).length
    if (fragments.length < 3 || spacedFragments < 2) continue
    const candidate = fragments.join('')
    if (/^[A-Za-z]{6,80}$/.test(candidate)) return candidate
  }
  return null
}

function cleanDuplicateTableForDisplay(pageSection: string): string {
  const recognizedHeading = RECOGNIZED_TABLE_HEADING_PATTERN.exec(pageSection)
  if (!recognizedHeading) return pageSection

  const beforeTable = pageSection.slice(0, recognizedHeading.index)
  const afterTable = pageSection.slice(recognizedHeading.index)
  const lines = beforeTable.split('\n')
  lines.forEach((line, index) => {
    if (line.trim() === '_本页未提取到除标题外的文字。_') lines[index] = ''
  })
  const pipeIndexes = lines
    .map((line, index) => (line.trimStart().startsWith('|') ? index : -1))
    .filter((index) => index >= 0)

  const removeIndexes = new Set<number>()
  if (pipeIndexes.length > 0) {
    let groupStart = pipeIndexes[0]
    let groupEnd = pipeIndexes[0]
    for (const index of [...pipeIndexes.slice(1), lines.length + 4]) {
      if (index - groupEnd <= 3) {
        groupEnd = index
        continue
      }
      for (let removeIndex = groupStart; removeIndex <= groupEnd; removeIndex += 1) {
        removeIndexes.add(removeIndex)
      }
      groupStart = index
      groupEnd = index
    }
  }

  const tableBody = afterTable.slice(recognizedHeading[0].length)
  const nextSectionIndex = tableBody.search(/^###\s+/m)
  const canonicalTable = nextSectionIndex < 0 ? tableBody : tableBody.slice(0, nextSectionIndex)
  const canonicalWords = new Set(tableWords(canonicalTable))
  lines.forEach((line, index) => {
    if (removeIndexes.has(index) || /^#{1,6}\s+/.test(line)) return
    const words = tableWords(line)
    if (words.length < 3) return
    const overlap = words.filter((word) => canonicalWords.has(word)).length
    if (overlap / words.length >= 0.72) removeIndexes.add(index)
  })
  lines.forEach((line, index) => {
    if (!/^\s*\d{1,3}\s*$/.test(line)) return
    if (removeIndexes.has(index - 1) || removeIndexes.has(index + 1)) {
      removeIndexes.add(index)
    }
  })

  const recoveredTitle = recoverFragmentedTableHeading(lines)
  const pageHeadingIndex = lines.findIndex((line) => /^## 第\s*\d+\s*页[^\n]*$/.test(line))
  if (recoveredTitle && pageHeadingIndex >= 0) {
    const currentTitle = lines[pageHeadingIndex].replace(/^## 第\s*\d+\s*页\s*-?\s*/, '')
    if (normalizeTableText(afterTable).includes(normalizeTableText(currentTitle))) {
      lines[pageHeadingIndex] = lines[pageHeadingIndex].replace(/^(## 第\s*\d+\s*页)(?:\s*-.*)?$/, `$1 - ${recoveredTitle}`)
    }
  }

  return `${lines.filter((_line, index) => !removeIndexes.has(index)).join('\n').trimEnd()}\n\n${afterTable}`
}

function hasReadablePageContent(contentBeforeOcr: string): boolean {
  // 图片功能说明、识别表格或正文已经能够表达页面含义时，不再展示原始 OCR 清单。
  if (/\*\*图片功能：\*\*|^### 本页识别表格\s*$/m.test(contentBeforeOcr)) {
    return true
  }

  const plainText = contentBeforeOcr
    .replace(/^## 第\s*\d+\s*页[^\n]*$/gm, '')
    .replace(/^### 本页重要图片\s*$/gm, '')
    .replace(/!\[[^\]]*\]\([^\n)]*\)/g, '')
    .replace(/^>\s*图片类型[^\n]*$/gm, '')
    .replace(/^_本页未提取到除标题外的文字。_$/gm, '')
    .replace(/\s+/g, '')

  return plainText.length >= 30
}

function cleanPageOcrForDisplay(pageSection: string): string {
  const ocrHeading = OCR_HEADING_PATTERN.exec(pageSection)
  if (!ocrHeading) return pageSection

  const beforeOcr = pageSection.slice(0, ocrHeading.index).trimEnd()
  const rawOcr = pageSection
    .slice(ocrHeading.index + ocrHeading[0].length)
    .replace(OCR_NOTICE_PATTERN, '')
    .trim()

  if (hasReadablePageContent(beforeOcr) || !rawOcr) {
    return `${beforeOcr}\n`
  }

  // 纯扫描页没有其他可读正文时，保留识别结果兜底，但去掉技术标题、警告和项目符号。
  const fallbackText = rawOcr
    .replace(/^-\s+/gm, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()

  return `${beforeOcr}\n\n${fallbackText}\n`
}

function cleanPageForDisplay(pageSection: string): string {
  return cleanPageOcrForDisplay(cleanDuplicateTableForDisplay(pageSection))
}

export function prepareMarkdownForDisplay(markdown: string): string {
  if (!markdown) return markdown
  if (!OCR_HEADING_PATTERN.test(markdown) && !RECOGNIZED_TABLE_HEADING_PATTERN.test(markdown)) return markdown

  const pageStarts = Array.from(markdown.matchAll(PAGE_HEADING_PATTERN), (match) => match.index ?? 0)
  if (pageStarts.length === 0) return cleanPageForDisplay(markdown)

  const sections: string[] = []
  if (pageStarts[0] > 0) sections.push(markdown.slice(0, pageStarts[0]))

  for (let index = 0; index < pageStarts.length; index += 1) {
    const start = pageStarts[index]
    const end = pageStarts[index + 1] ?? markdown.length
    sections.push(cleanPageForDisplay(markdown.slice(start, end)))
  }

  return sections.join('').replace(/\n{4,}/g, '\n\n\n')
}

export function renderMarkdownForDisplay(
  markdown: string,
  renderMarkdown: (value: string) => string,
): string {
  const mergedTables: string[] = []
  const prepared = prepareMarkdownForDisplay(markdown)
  const withPlaceholders = prepared.replace(
    MERGED_TABLE_PATTERN,
    (whole, tableSource: string, metadataSource: string) => {
      const rendered = renderMergedTable(tableSource, metadataSource)
      if (!rendered) return whole
      const index = mergedTables.push(rendered) - 1
      return `\n\nRAGMERGEDTABLEPLACEHOLDER${index}END\n\n`
    },
  )
  let html = renderMarkdown(withPlaceholders)
  mergedTables.forEach((table, index) => {
    const placeholder = `RAGMERGEDTABLEPLACEHOLDER${index}END`
    html = html.replace(`<p>${placeholder}</p>`, table)
  })
  return html
}
