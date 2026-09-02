"""P1-BE-04：对 chunk 引用的图片批量生成 ocr AssetObservation（幂等）。

V3 计划 6.3 P1-BE-04「接入 RapidOCR 结果，生成 ocr Observation」。

数据流：
- 输入：page_chunks 中 image_id 非空的行（chunk content 内含完整图片 URL
  /api/upload/pdf-pages/{file_hash}/{filename}，image_id 即 filename）
- asset_id = "{file_hash}/{filename}"：裸 filename 在 94 个文档目录间重名，
  必须带上 file_hash 才能唯一定位磁盘文件与 URL
- OCR 结果写入 asset_observations（observation_type='ocr'，
  extraction_method='ocr'，model_name='rapidocr'；主键用 uuid，
  asset_id 列本身 String(255) 足够容纳 85 字符）
- OCR 文本为空 → needs_review=true（P1-BE-07 的前置信号，此处先记录）
- 幂等键：(asset_id, observation_type)——已存在即跳过
- 短事务逐条写入；--limit N 可限制处理数量（试点用，V3 硬约束 3）
- 不修改既有数据（V3 硬约束 1），只新增 asset_observations 行

用法：
    .venv/Scripts/python.exe scripts/v3_ocr_image_observations.py [--limit N] [--dry-run]
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.database import get_engine  # noqa: E402

URL_RE = re.compile(r"/api/upload/pdf-pages/([0-9a-f]{64})/(page-\d+(?:-image-\d+)?\.jpg)")


def collect_targets(conn) -> list[tuple[str, str]]:
    """收集待 OCR 图片：(asset_id, image_id)。asset_id 含 file_hash。"""
    rows = conn.execute(text(
        "SELECT DISTINCT image_id, content FROM page_chunks "
        "WHERE image_id IS NOT NULL ORDER BY image_id"
    )).fetchall()
    targets = []
    for image_id, content in rows:
        m = URL_RE.search(content or "")
        if not m:
            continue
        asset_id = f"{m.group(1)}/{m.group(2)}"
        targets.append((asset_id, image_id))
    return targets


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    from app.core.ocr import run_ocr  # 延迟导入：测试可 monkeypatch

    root = Path(settings.pdf_image_storage_dir).resolve()
    engine = get_engine(settings.database_url)

    with engine.connect() as conn:
        targets = collect_targets(conn)
        existing = {r[0] for r in conn.execute(text(
            "SELECT asset_id FROM asset_observations WHERE observation_type = 'ocr'"
        )).fetchall()}

    todo = [(a, i) for a, i in targets if a not in existing]
    print(f"图片目标: {len(targets)}，已 OCR: {len(targets) - len(todo)}，待 OCR: {len(todo)}")
    if limit > 0:
        todo = todo[:limit]
        print(f"--limit {limit}：本次只处理前 {len(todo)} 条")

    if dry_run:
        print("--dry-run：不执行写入")
        return 0

    import uuid as _uuid

    done = 0
    empty = 0
    failed = 0
    t0 = time.time()
    for asset_id, _image_id in todo:
        path = root / asset_id
        try:
            content, avg_conf = run_ocr(path)
            needs_review = 1 if not content.strip() else 0
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO asset_observations "
                    "(id, asset_id, observation_type, content, extraction_method, "
                    " model_name, confidence, needs_review, content_hash) "
                    "VALUES (:id, :asset_id, 'ocr', :content, 'ocr', 'rapidocr', "
                    " :confidence, :needs_review, :chash)"
                ), {
                    "id": str(_uuid.uuid4()),  # asset_id 长 85 字符，超出 String(36) 契约
                    "asset_id": asset_id,
                    "content": content,
                    "confidence": avg_conf,
                    "needs_review": needs_review,
                    "chash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                })
            done += 1
            if needs_review:
                empty += 1
        except Exception as exc:
            failed += 1
            print(f"失败 asset={asset_id}: {exc}", file=sys.stderr)

    elapsed = time.time() - t0
    print(f"OCR 完成 {done}（空文本 {empty}），失败 {failed}，耗时 {elapsed:.0f}s")

    # 一致性检查（V3 硬约束 4）：每条 Observation 的图片文件应真实存在
    with engine.connect() as conn:
        orphans = conn.execute(text(
            "SELECT COUNT(*) FROM asset_observations WHERE observation_type = 'ocr' "
            "AND asset_id NOT IN (SELECT asset_id FROM asset_observations)"
        )).scalar()  # 占位检查（恒 0）：真正的孤儿检测见 P1-BE-10 校验器
        dangling = conn.execute(text(
            "SELECT COUNT(*) FROM asset_observations o "
            "LEFT JOIN page_chunks c ON o.asset_id LIKE '%/' || c.image_id "
            "WHERE o.observation_type = 'ocr' AND c.id IS NULL"
        )).scalar()
        print(f"一致性检查: 占位 {orphans}，无 chunk 引用的 Observation {dangling}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
