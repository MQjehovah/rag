"""P1-BE-05：对 table chunk 生成结构化 JSON Observation（幂等）。

V3 计划 6.3 P1-BE-05「对表格生成结构化 Markdown/JSON Observation」。

数据流：
- 输入：page_chunks 中 content_type='table' 的行（PDF 混合解析产出的
  Markdown 表格，破碎程度不一：行内断裂、无表头、纯文本行混入）
- 解析：识别「表头行 + 分隔行（---）」对，其后连续管道行构成数据行；
  非管道行中断表格；一个 chunk 可解析出多个表
- 输出：asset_observations（observation_type='table'，extraction_method='parser'）：
  content = {"tables": [{"headers": [...], "rows": [[...]]}]}
  解析不出任何表（无 header+sep+data 结构）→ needs_review=1，content 保留 tables=[]
- asset_id = chunk id：表格是 chunk 级内容而非图片资产，P2 卡片生成按 chunk 关联
- 幂等键：(asset_id, observation_type='table') 已存在即跳过

用法：
    .venv/Scripts/python.exe scripts/v3_table_observations.py [--dry-run]
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.table_parser import parse_markdown_tables  # noqa: E402
from app.models.database import get_engine  # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    import uuid

    engine = get_engine(settings.database_url)

    with engine.connect() as conn:
        chunks = conn.execute(text(
            "SELECT id, content FROM page_chunks WHERE content_type = 'table' ORDER BY page_id, chunk_index"
        )).fetchall()
        existing = {r[0] for r in conn.execute(text(
            "SELECT asset_id FROM asset_observations WHERE observation_type = 'table'"
        )).fetchall()}

    todo = [(cid, c) for cid, c in chunks if cid not in existing]
    print(f"table chunks: {len(chunks)}，已转换: {len(chunks) - len(todo)}，待转换: {len(todo)}")
    if limit > 0:
        todo = todo[:limit]
        print(f"--limit {limit}：本次只处理前 {len(todo)} 条")

    if dry_run:
        print("--dry-run：不执行写入")
        return 0

    inserted = 0
    unparsed = 0
    for chunk_id, content in todo:
        tables = parse_markdown_tables(content or "")
        needs_review = 0 if tables else 1
        if not tables:
            unparsed += 1
        payload = json.dumps({"tables": tables}, ensure_ascii=False)
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO asset_observations "
                    "(id, asset_id, observation_type, content, extraction_method, "
                    " model_name, confidence, needs_review, content_hash) "
                    "VALUES (:id, :asset_id, 'table', :content, 'parser', "
                    " NULL, :confidence, :needs_review, :chash)"
                ), {
                    "id": str(uuid.uuid4()),
                    "asset_id": chunk_id,
                    "content": payload,
                    "confidence": 1.0 if tables else 0.0,
                    "needs_review": needs_review,
                    "chash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                })
            inserted += 1
        except Exception as exc:
            print(f"失败 chunk_id={chunk_id}: {exc}", file=sys.stderr)

    print(f"插入 {inserted} 条 table Observation（不可解析 {unparsed} 条标记 needs_review）")

    # 一致性检查：table Observation 的 asset_id 必须对应存在的 table chunk
    with engine.connect() as conn:
        dangling = conn.execute(text(
            "SELECT COUNT(*) FROM asset_observations o "
            "LEFT JOIN page_chunks c ON o.asset_id = c.id "
            "WHERE o.observation_type = 'table' AND c.id IS NULL"
        )).scalar()
        dup = conn.execute(text(
            "SELECT COUNT(*) FROM (SELECT asset_id FROM asset_observations "
            "WHERE observation_type='table' GROUP BY asset_id HAVING COUNT(*) > 1)"
        )).scalar()
        print(f"一致性检查: 悬空 chunk 引用 {dangling}，重复 asset_id {dup}")
        if dangling or dup:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
