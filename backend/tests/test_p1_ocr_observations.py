"""P1-BE-04：RapidOCR Observation 生成测试。

用假 OCR（monkeypatch run_ocr）在临时库验证批量脚本的幂等、
needs_review 标记、asset_id 构造；另含一个真实 RapidOCR 冒烟测试
（带 PIL 生成图片，无网络依赖，模型已本地缓存）。
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.database import Page, PageChunk, get_engine, init_db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "v3_ocr_image_observations.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("v3_ocr_image_observations", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_page_with_image_chunk(db, file_hash: str, image_name: str, extra_prefix=""):
    page = Page(id=str(uuid.uuid4()), title="t", content="c")
    db.add(page)
    db.flush()
    url = f"/api/upload/pdf-pages/{file_hash}/{image_name}"
    chunk = PageChunk(
        id=str(uuid.uuid4()), page_id=page.id, chunk_index=0,
        content=f"{extra_prefix}![img]({url})", content_type="image_caption",
        page_number=1, image_id=image_name,
    )
    db.add(chunk)
    db.commit()
    return page, chunk


@pytest.fixture()
def ocr_db(tmp_path, monkeypatch):
    """临时库 + 2 个 chunk（1 个有 URL、1 个无 URL），settings 指向临时库。"""
    url = f"sqlite:///{(tmp_path / 'ocr_test.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    h1 = "b" * 64
    _make_page_with_image_chunk(db, h1, "page-1-image-1.jpg")
    # 无 URL 的 chunk（image_id 有值但 content 无 URL）→ 不应被处理
    page2 = Page(id=str(uuid.uuid4()), title="t2", content="c2")
    db.add(page2)
    db.flush()
    db.add(PageChunk(
        id=str(uuid.uuid4()), page_id=page2.id, chunk_index=0,
        content="无图片链接", content_type="image_caption", image_id="page-9-image-1.jpg",
    ))
    db.commit()
    db.close()
    engine.dispose()

    monkeypatch.setattr("app.config.settings.database_url", url)
    monkeypatch.setattr("app.config.settings.pdf_image_storage_dir", str(tmp_path / "imgs"))
    imgs = tmp_path / "imgs" / h1
    imgs.mkdir(parents=True)
    (imgs / "page-1-image-1.jpg").write_bytes(b"fake-jpeg-bytes")

    monkeypatch.setattr("app.core.ocr.run_ocr", lambda path: ("启动 按钮", 0.97))
    yield url, h1


def _observations(url):
    engine = get_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, asset_id, observation_type, content, extraction_method, "
            "model_name, confidence, needs_review, content_hash FROM asset_observations"
        )).fetchall()
    engine.dispose()
    return rows


def test_ocr_writes_observation(ocr_db):
    url, h1 = ocr_db
    mod = _load_script()
    assert mod.main() == 0

    rows = _observations(url)
    assert len(rows) == 1
    row = rows[0]
    assert row[1] == f"{h1}/page-1-image-1.jpg"
    assert row[2] == "ocr"
    assert row[3] == "启动 按钮"
    assert row[4] == "ocr"
    assert row[5] == "rapidocr"
    assert row[6] == 0.97
    assert row[7] == 0
    import hashlib
    assert row[8] == hashlib.sha256("启动 按钮".encode("utf-8")).hexdigest()


def test_ocr_idempotent(ocr_db):
    url, h1 = ocr_db
    mod = _load_script()
    assert mod.main() == 0
    assert mod.main() == 0
    assert len(_observations(url)) == 1


def test_ocr_empty_text_needs_review(ocr_db, monkeypatch):
    """OCR 无文本 → needs_review=true（P1-BE-07 前置信号）。"""
    url, h1 = ocr_db
    monkeypatch.setattr("app.core.ocr.run_ocr", lambda path: ("", 0.0))
    mod = _load_script()
    assert mod.main() == 0

    rows = _observations(url)
    assert len(rows) == 1
    assert rows[0][7] == 1  # needs_review
    assert rows[0][3] == ""


def test_dry_run_no_write(ocr_db):
    url, h1 = ocr_db
    mod = _load_script()
    import sys
    sys.argv.append("--dry-run")
    try:
        assert mod.main() == 0
    finally:
        sys.argv.remove("--dry-run")
    assert len(_observations(url)) == 0


def test_skips_chunks_without_url(ocr_db):
    """image_id 有值但 content 无 URL → 不处理（asset_id 无法构造）。"""
    url, h1 = ocr_db
    mod = _load_script()
    assert mod.main() == 0
    rows = _observations(url)
    assert all("page-9" not in r[1] for r in rows)


def test_real_rapidocr_smoke():
    """真实 RapidOCR 冒烟：PIL 画文字图，无网络，模型已缓存。"""
    pytest.importorskip("rapidocr")
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont
    from app.core.ocr import run_ocr

    img = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("msyh.ttc", 48)  # Windows 微软雅黑
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 30), "启动按钮", fill="black", font=font)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ocr_smoke.png"
        img.save(p)
        content, conf = run_ocr(p)
    assert "启动" in content
    assert conf > 0.5
