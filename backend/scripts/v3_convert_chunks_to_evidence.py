"""P1-BE-02：将现有 PageChunk 幂等转换为文本 Evidence（一次性 + 可重复执行）。

V3 计划 6.3 P1-BE-02「将现有 PageChunk 转换为文本 Evidence，保证幂等」；
6.3 P1-BE-03「将 PDF 页码、content_type、image_id 写入 Evidence locator」
（locator 字段一并在此写入）。

转换规则：
- 每条 page_chunk → 一条 evidence_items（evidence_type 按 content_type 映射：
  text→text、image_caption→text、table→table；image 类 Evidence 待 P1-BE-04
  接入 OCR 后由 Observation 生成，不在此转换）
- locator_json = {page_number, content_type, image_id, chunk_index, heading}
  （heading 取 chunk 内首个 ## 标题，与检索结果展示口径一致）
- 幂等键：source_chunk_id——已转换过的 chunk 跳过。content_hash 建索引供
  P1-BE-02 后续按内容去重查询，但同内容不同位置是合法的，不建唯一约束。
- 短事务（P0-BE-06）：逐 chunk 独立 INSERT，单条失败不回滚整体进度。

用法：
    .venv/Scripts/python.exe scripts/v3_convert_chunks_to_evidence.py [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.database import get_engine  # noqa: E402

# content_type → evidence_type 映射（image_caption 本质是文本描述，归 text）
TYPE_MAP = {"text": "text", "image_caption": "text", "table": "table"}


def build_locator(chunk) -> str:
    """P1-BE-03：页码/content_type/image_id/chunk_index/heading 写入 locator_json。"""
    chunk_id, chunk_index, content_type, page_number, image_id, content = chunk
    heading = ""
    for line in (content or "").splitlines():
        line = line.strip()
        if line.startswith("## "):
            heading = line.lstrip("# ").strip()
            break
    locator = {
        "chunk_index": chunk_index,
        "content_type": content_type,
    }
    if page_number is not None:
        locator["page_number"] = page_number
    if image_id:
        locator["image_id"] = image_id
    if heading:
        locator["heading"] = heading[:255]
    return json.dumps(locator, ensure_ascii=False)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    engine = get_engine(settings.database_url)

    with engine.connect() as conn:
        chunks = conn.execute(text(
            "SELECT id, chunk_index, content_type, page_number, image_id, content "
            "FROM page_chunks ORDER BY page_id, chunk_index"
        )).fetchall()
        existing = {r[0] for r in conn.execute(
            text("SELECT source_chunk_id FROM evidence_items WHERE source_chunk_id IS NOT NULL")
        ).fetchall()}

    total = len(chunks)
    todo = [c for c in chunks if c[0] not in existing]
    print(f"page_chunks 总数: {total}，已转换: {total - len(todo)}，待转换: {len(todo)}")

    if dry_run:
        print("--dry-run：不执行写入")
        return 0

    inserted = 0
    failed = 0
    for chunk in todo:
        chunk_id, chunk_index, content_type, page_number, image_id, content = chunk
        evidence_type = TYPE_MAP.get(content_type or "text", "text")
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO evidence_items "
                    "(id, source_page_id, source_chunk_id, evidence_type, content, "
                    " locator_json, content_hash, source_doc_hash, extraction_method, confidence, needs_review, status) "
                    "VALUES (:id, (SELECT page_id FROM page_chunks WHERE id = :chunk_id), "
                    " :chunk_id, :etype, :content, :locator, :chash, "
                    " (SELECT p.content_hash FROM page_chunks c JOIN pages p ON p.id = c.page_id WHERE c.id = :chunk_id), "
                    " 'parser', 1.0, 0, 'active')"
                ), {
                    "id": chunk_id,  # evidence 复用 chunk id：天然幂等 + 引用直达
                    "chunk_id": chunk_id,
                    "etype": evidence_type,
                    "content": content or "",
                    "locator": build_locator(chunk),
                    "chash": hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
                })
            inserted += 1
        except Exception as exc:
            failed += 1
            print(f"失败 chunk_id={chunk_id}: {exc}", file=sys.stderr)

    print(f"插入 {inserted} 条 Evidence，失败 {failed} 条")

    # 数据一致性检查（V3 硬约束 4）
    with engine.connect() as conn:
        orphans = conn.execute(text(
            "SELECT COUNT(*) FROM evidence_items e "
            "LEFT JOIN page_chunks c ON e.source_chunk_id = c.id "
            "WHERE e.source_chunk_id IS NOT NULL AND c.id IS NULL"
        )).scalar()
        missing_page = conn.execute(text(
            "SELECT COUNT(*) FROM evidence_items e "
            "LEFT JOIN pages p ON e.source_page_id = p.id "
            "WHERE p.id IS NULL"
        )).scalar()
        print(f"一致性检查: 悬空 chunk 引用 {orphans}，悬空 page 引用 {missing_page}")
        if orphans or missing_page:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
