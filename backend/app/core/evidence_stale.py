"""Evidence 文档 stale 判定（P1-BE-08）。

V3 计划 6.3 P1-BE-08：文档 Hash 变化时，将旧 Evidence 标记 stale，
不直接物理删除（保留追溯，硬约束 1/5）。

判定依据：Evidence 生成时记录来源文档（page）的 content_hash 快照
（source_doc_hash）。当前 page.content_hash 与快照不一致 → 文档已更新
→ 旧 Evidence 内容不再对应现行文档 → stale。

为什么用 page.content_hash 而非 source_file_hash / 图片目录 hash：
- Evidence 由 page.content 分块转换而来（P1-BE-02），content_hash 是
  其直接内容指纹；
- source_file_hash 是 PDF 原始字节哈希，与「Evidence 内容」粒度不对齐；
- 图片目录 hash 与 page 字段本就不对应（P1-BE-04 已实证 0/94 匹配）。
"""
from __future__ import annotations


def is_stale(source_doc_hash: str | None, current_doc_hash: str | None) -> bool:
    """文档快照哈希与当前哈希不一致 → stale。

    Args:
        source_doc_hash: Evidence 生成时的文档 content_hash 快照。
        current_doc_hash: 当前 page.content_hash。

    Returns:
        source_doc_hash 为空 → False（无法判定，视为未变化，避免误标）。
        否则 True 当且仅当两者不相等。
    """
    if not source_doc_hash:
        return False
    return source_doc_hash != current_doc_hash
