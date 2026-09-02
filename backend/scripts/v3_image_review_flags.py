"""P1-BE-07：对图片统一判定 needs_review 并标记（幂等）。

V3 计划 6.3 P1-BE-07「图片只有 URL、OCR 为空、无功能说明时标记 needs_review=true」。

判定：
- 以每张图片的 ocr Observation 为锚点（P1-BE-04 已为 1358 张 distinct 图片建立）
- OCR 为空 = ocr Observation.content 为空
- 无功能说明 = 该 asset_id 无 ui_function/operation_flow/manual Observation
- needs_review = OCR 为空 且 无功能说明（app/core/image_review.py）

落点：UPDATE 该 ocr Observation 行的 needs_review（图片级复核信号，P1-FE-03 读取）。
幂等：needs_review 为目标值即跳过，重复执行结果一致。

与 P1-BE-04 的差异：P1-BE-04 将「OCR 空」一律标 needs_review=1；
本脚本补上「无功能说明」维度，未来人工补 manual 说明（P1-FE-04）后自动解除。

用法：
    .venv/Scripts/python.exe scripts/v3_image_review_flags.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.image_review import FUNCTION_OBSERVATION_TYPES, image_needs_review  # noqa: E402
from app.models.database import get_engine  # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    engine = get_engine(settings.database_url)

    with engine.connect() as conn:
        ocr_rows = conn.execute(text(
            "SELECT id, asset_id, content, needs_review FROM asset_observations "
            "WHERE observation_type = 'ocr'"
        )).fetchall()
        placeholders = ",".join(f":t{i}" for i in range(len(FUNCTION_OBSERVATION_TYPES)))
        func_assets = {r[0] for r in conn.execute(text(
            f"SELECT DISTINCT asset_id FROM asset_observations "
            f"WHERE observation_type IN ({placeholders})"
        ), {f"t{i}": t for i, t in enumerate(FUNCTION_OBSERVATION_TYPES)}).fetchall()}

    updated = 0
    already = 0
    flagged = 0
    for obs_id, asset_id, content, current in ocr_rows:
        target = 1 if image_needs_review(content or "", asset_id in func_assets) else 0
        if target == (1 if current else 0):
            already += 1
        else:
            if not dry_run:
                with engine.begin() as conn:
                    conn.execute(text(
                        "UPDATE asset_observations SET needs_review = :nr WHERE id = :id"
                    ), {"nr": target, "id": obs_id})
            updated += 1
        if target:
            flagged += 1

    print(
        f"图片(ocr Observation)总数: {len(ocr_rows)}，"
        f"有功能说明: {len(func_assets)}，待复核(needs_review=true): {flagged}"
    )
    print(f"更新 {updated} 行，无需变更 {already} 行" + ("（--dry-run 未写库）" if dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
