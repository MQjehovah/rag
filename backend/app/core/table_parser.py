"""Markdown 表格解析器：chunk 文本 → 结构化 {headers, rows}（P1-BE-05）。

输入是 PDF 混合解析产出的 Markdown 表格，破碎程度不一：完整表
（表头 + --- 分隔行 + 数据行）、仅数据行有管道符、破碎 OCR 文本混入。
本解析器只提取「规范结构」（表头 + 分隔行 + ≥1 数据行）；
解析不出结构是常态而非异常——调用方据此标记 needs_review（P1-BE-07）。
"""
from __future__ import annotations


def split_row(line: str) -> list[str]:
    """'| a | b |' → ['a', 'b']；保留内部空单元格以维持列对齐，cell 内 <br> 原样保留。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    """'| --- | :---: |' 这类分隔行：只含 |、-、:、空白。"""
    stripped = line.strip()
    if "|" not in stripped or "-" not in stripped:
        return False
    return set(stripped) <= set("|-: ")


def parse_markdown_tables(content: str) -> list[dict]:
    """从 chunk 文本解析所有规范 markdown 表。

    规则（GFM 变体，适配 OCR 破碎数据）：
    - 「表头行 + 分隔行」开启一个表；其后连续含管道符的行是数据行
    - 非管道行或空行中断表格；数据行本身长得像分隔行时也中断
      （相邻两个表无空行分隔的场景，拆成两个表更利于 P2 卡片生成）
    - 全空数据行（'| | |'）跳过
    """
    lines = (content or "").splitlines()
    tables: list[dict] = []
    i = 0
    while i < len(lines):
        if (
            "|" in lines[i]
            and i + 1 < len(lines)
            and is_separator(lines[i + 1])
        ):
            headers = split_row(lines[i])
            j = i + 2
            rows: list[list[str]] = []
            while (
                j < len(lines)
                and "|" in lines[j]
                and lines[j].strip()
                and not is_separator(lines[j])
            ):
                cells = split_row(lines[j])
                if any(c != "" for c in cells):
                    rows.append(cells)
                j += 1
            if any(h != "" for h in headers) and rows:
                tables.append({"headers": headers, "rows": rows})
            i = j
        else:
            i += 1
    return tables
