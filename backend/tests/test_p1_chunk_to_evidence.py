"""P1-BE-02：PageChunk→Evidence 幂等转换测试。

调用 scripts/v3_convert_chunks_to_evidence.py 的 main()（--dry-run 与全量），
在临时文件库上验证：全量转换、重复执行不重复插入、locator 完整、
类型映射、失败一致性检查返回码。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.database import Page, PageChunk, get_engine, init_db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "v3_convert_chunks_to_evidence.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("v3_convert_chunks_to_evidence", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def conv_db(tmp_path, monkeypatch):
    """临时文件库 + 少量 chunk 数据，settings.database_url 指向它。"""
    url = f"sqlite:///{(tmp_path / 'conv_test.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    page = Page(id="page-1", title="t", content="正文")
    db.add(page)
    db.flush()
    db.add_all([
        PageChunk(id="c-text", page_id="page-1", chunk_index=0, content="普通文本块", content_type="text", page_number=3),
        PageChunk(id="c-tab", page_id="page-1", chunk_index=1, content="## 第 3 页 - 规格\n\n| a | b |", content_type="table", page_number=3),
        PageChunk(id="c-img", page_id="page-1", chunk_index=2, content="图片描述", content_type="image_caption", page_number=4, image_id="page-4-image-1.jpg"),
    ])
    db.commit()
    db.close()
    engine.dispose()

    monkeypatch.setattr("app.config.settings.database_url", url)
    yield url
    # engine 由脚本自建自关，无需额外清理


def test_locator_fields(conv_db):
    """P1-BE-03：locator_json 含 page_number/content_type/image_id/heading。"""
    mod = _load_script()
    assert mod.main() == 0

    engine = get_engine(conv_db)
    with engine.connect() as conn:
        rows = {r[0]: r for r in conn.execute(text(
            "SELECT source_chunk_id, locator_json FROM evidence_items"
        )).fetchall()}
    engine.dispose()

    loc_text = json.loads(rows["c-text"][1])
    assert loc_text["page_number"] == 3
    assert loc_text["content_type"] == "text"
    assert loc_text["chunk_index"] == 0
    assert "image_id" not in loc_text

    loc_tab = json.loads(rows["c-tab"][1])
    assert loc_tab["heading"] == "第 3 页 - 规格"

    loc_img = json.loads(rows["c-img"][1])
    assert loc_img["image_id"] == "page-4-image-1.jpg"
    assert loc_img["page_number"] == 4


def test_type_mapping(conv_db):
    """text→text、table→table、image_caption→text。"""
    mod = _load_script()
    assert mod.main() == 0

    engine = get_engine(conv_db)
    with engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT source_chunk_id, evidence_type FROM evidence_items"
        )).fetchall())
    engine.dispose()

    assert types == {"c-text": "text", "c-tab": "table", "c-img": "text"}


def test_idempotent_rerun(conv_db):
    """重复执行不重复插入（幂等）。"""
    mod = _load_script()
    assert mod.main() == 0
    # 第二次运行：待转换应为 0，插入 0
    assert mod.main() == 0

    engine = get_engine(conv_db)
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM evidence_items")).scalar()
    engine.dispose()
    assert n == 3


def test_dry_run_no_write(conv_db):
    mod = _load_script()
    sys.argv.append("--dry-run")
    try:
        assert mod.main() == 0
    finally:
        sys.argv.remove("--dry-run")

    engine = get_engine(conv_db)
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM evidence_items")).scalar()
    engine.dispose()
    assert n == 0


def test_evidence_id_equals_chunk_id(conv_db):
    """Evidence 复用 chunk id：引用直达、天然幂等。"""
    mod = _load_script()
    assert mod.main() == 0

    engine = get_engine(conv_db)
    with engine.connect() as conn:
        ids = {r[0] for r in conn.execute(text("SELECT id FROM evidence_items")).fetchall()}
    engine.dispose()
    assert ids == {"c-text", "c-tab", "c-img"}


def test_content_hash_matches(conv_db):
    mod = _load_script()
    assert mod.main() == 0

    engine = get_engine(conv_db)
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT content_hash, content FROM evidence_items WHERE id = 'c-text'"
        )).fetchone()
    engine.dispose()

    import hashlib
    assert row[0] == hashlib.sha256(row[1].encode("utf-8")).hexdigest()
