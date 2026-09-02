"""P1-BE-07：图片 needs_review 判定 + 标记测试。

判定单测覆盖四种组合，重点验证「空 OCR 但有功能说明 → 不标 needs_review」
（这是相对 P1-BE-04「OCR 空一律标」的修正增量）。
脚本集成测试用临时库验证 UPDATE 落点、幂等、dry-run。
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.image_review import FUNCTION_OBSERVATION_TYPES, image_needs_review
from app.models.database import get_engine, init_db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "v3_image_review_flags.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("v3_image_review_flags", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 判定函数单测 ----------

def test_empty_ocr_no_function_needs_review():
    assert image_needs_review("", False) is True
    assert image_needs_review("   ", False) is True
    assert image_needs_review(None, False) is True


def test_empty_ocr_with_function_not_review():
    """空 OCR 但已有功能说明（P1-FE-04 人工补 manual）→ 不需复核。"""
    assert image_needs_review("", True) is False


def test_nonempty_ocr_no_function_not_review():
    assert image_needs_review("启动按钮", False) is False


def test_nonempty_ocr_with_function_not_review():
    assert image_needs_review("启动按钮", True) is False


def test_function_observation_types_cover_vlm_and_manual():
    assert set(FUNCTION_OBSERVATION_TYPES) == {"ui_function", "operation_flow", "manual"}


# ---------- 脚本集成测试 ----------

def _add_observation(conn, asset_id, obs_type, content, needs_review=0):
    conn.execute(text(
        "INSERT INTO asset_observations "
        "(id, asset_id, observation_type, content, extraction_method, needs_review) "
        "VALUES (:id, :asset_id, :otype, :content, 'ocr', :nr)"
    ), {
        "id": str(uuid.uuid4()),
        "asset_id": asset_id,
        "otype": obs_type,
        "content": content,
        "nr": needs_review,
    })


@pytest.fixture()
def review_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'review_test.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    with engine.begin() as conn:
        # A：空 OCR + 无功能说明 → 需复核（应标 1）
        _add_observation(conn, "hA/page-1.jpg", "ocr", "")
        # B：空 OCR + 有 manual 说明 → 不需复核（关键增量，应保持 0）
        _add_observation(conn, "hB/page-1.jpg", "ocr", "")
        _add_observation(conn, "hB/page-1.jpg", "manual", "这是水箱", needs_review=0)
        # C：有 OCR + 无功能说明 → 不需复核
        _add_observation(conn, "hC/page-1.jpg", "ocr", "启动按钮")
        # D：有 OCR + 有 ui_function 说明 → 不需复核
        _add_observation(conn, "hD/page-1.jpg", "ocr", "零件图")
        _add_observation(conn, "hD/page-1.jpg", "ui_function", "示意装配顺序")
    engine.dispose()
    monkeypatch.setattr("app.config.settings.database_url", url)
    return url


def _needs_review_map(url):
    engine = get_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT asset_id, needs_review FROM asset_observations "
            "WHERE observation_type = 'ocr'"
        )).fetchall()
    engine.dispose()
    return {r[0]: r[1] for r in rows}


def test_review_flags_only_empty_ocr_without_function(review_db):
    url = review_db
    mod = _load_script()
    assert mod.main() == 0

    m = _needs_review_map(url)
    assert m["hA/page-1.jpg"] == 1   # 空 OCR + 无说明 → 标 1
    assert m["hB/page-1.jpg"] == 0   # 空 OCR + 有说明 → 保持 0
    assert m["hC/page-1.jpg"] == 0   # 有 OCR → 保持 0
    assert m["hD/page-1.jpg"] == 0   # 有 OCR + 有说明 → 保持 0


def test_review_flags_idempotent(review_db):
    url = review_db
    mod = _load_script()
    assert mod.main() == 0
    first = _needs_review_map(url)
    assert mod.main() == 0
    assert _needs_review_map(url) == first


def test_review_flags_corrects_prior_false_flag(review_db):
    """P1-BE-04 曾把「空 OCR + 无说明」误标 needs_review=0 → 脚本修正为 1。"""
    url = review_db
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE asset_observations SET needs_review = 0 "
            "WHERE asset_id = 'hA/page-1.jpg' AND observation_type = 'ocr'"
        ))
    engine.dispose()

    mod = _load_script()
    assert mod.main() == 0
    assert _needs_review_map(url)["hA/page-1.jpg"] == 1


def test_review_flags_dry_run_no_write(review_db):
    url = review_db
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE asset_observations SET needs_review = 0"))
    engine.dispose()

    mod = _load_script()
    import sys
    sys.argv.append("--dry-run")
    try:
        assert mod.main() == 0
    finally:
        sys.argv.remove("--dry-run")

    # dry-run 后所有行仍为 0
    assert all(v == 0 for v in _needs_review_map(url).values())
