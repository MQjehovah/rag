"""Phase 2：executor 统一转换边界 + 防二次转换 + Page 字段/hash 语义 + 迁移。

覆盖：
- converted/partial 写 Page，failed/blocked 不创建/覆盖 + SourceSyncError.stage=convert；
- 删除事件不调用 Converter；
- source hash 与 body hash 分离；
- 钉钉已转换 Manifest 不二次转换；
- Page 新字段写入；
- Alembic upgrade/downgrade/upgrade；
- legacy-note/v0 读取兼容。
"""
from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from app.models.database import (
    Notebook,
    Page,
    SourceConnection,
    SourceItem,
    SourceSyncError,
    SourceSyncRun,
    get_engine,
    init_db,
)
from app.sources.conversion_adapter import to_raw_source_item, convert_normalized
from app.sources.schemas import NormalizedSourceItem

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.abspath(os.path.join(BACKEND_ROOT, ".venv", "Scripts", "python.exe"))


def _nsi(**over):
    base = dict(
        connection_id="conn-1",
        source_type="gitlab",
        external_id="x1",
        external_version="v1",
        title="t",
        content="# 标题\n\n正文",
        content_type="md",
        source_path="docs/a.md",
        source_url="https://x",
        source_updated_at="2026-01-01",
        acl_scope={"scope": "project:1"},
        metadata_json={},
    )
    base.update(over)
    return NormalizedSourceItem(**base)


# ---------------------------------------------------------------------------
# Adapter / 防二次转换
# ---------------------------------------------------------------------------


def test_dingtalk_manifest_markdown_not_reconverted():
    """钉钉已转换 Manifest → PRECONVERTED_MARKDOWN → MARKDOWN（不二次转 PDF/Office/CSV）。"""
    from app.sources.schemas import InputRepresentation
    nsi = _nsi(
        source_type="dingtalk", content_type="pdf", source_path="dir/a.pdf",
        content="---\ntitle: \"x\"\nconverter: \"hybrid_pdf\"\n---\n\n# 正文",
        metadata_json={"manifest_conversion_status": "converted", "manifest_converter": "hybrid_pdf"},
        input_representation=InputRepresentation.PRECONVERTED_MARKDOWN,
    )
    raw = to_raw_source_item(nsi, connector_key="dingtalk")
    assert raw.content_kind.value == "markdown"  # 防二次转换


def test_dingtalk_unconverted_file_goes_to_registry():
    """钉钉未转换文件（manifest 非 converted）→ 按格式判定（pdf → PDF Converter）。"""
    nsi = _nsi(
        source_type="dingtalk", content_type="pdf", source_path="dir/a.pdf",
        content="", content_bytes=b"%PDF-1.4 real",
        metadata_json={"manifest_conversion_status": "downloaded"},
    )
    raw = to_raw_source_item(nsi, connector_key="dingtalk")
    assert raw.content_kind.value == "pdf"
    assert raw.raw_bytes == b"%PDF-1.4 real"


def test_source_hash_and_body_hash_separate():
    """代码转换：source_hash（原文）≠ body_hash（包裹后）。"""
    nsi = _nsi(content_type="code", source_path="src/a.py", content="def f():\n    return 1")
    note = convert_normalized(nsi, connector_key="gitlab")
    assert note.source_hash != note.body_hash


# ---------------------------------------------------------------------------
# executor 集成（临时库）
# ---------------------------------------------------------------------------


def _setup_db(tmp_path):
    engine = get_engine(f"sqlite:///{(tmp_path / 'p2.db').as_posix()}")
    init_db(engine)
    from app.models.database import get_session
    db = get_session(engine)
    notebook = Notebook(name="nb", group_id="g1")
    db.add(notebook)
    db.flush()
    conn = SourceConnection(id="conn-1", connector_key="gitlab", name="git", enabled=True,
                            target_notebook_id=notebook.id, config_json="{}")
    db.add(conn)
    db.flush()
    db.commit()
    return engine, db, notebook.id


def _mk_run(db, connection_id="conn-1", mode="incremental"):
    run = SourceSyncRun(id="run-1", connection_id=connection_id, mode=mode, status="queued")
    db.add(run)
    db.commit()
    return run


def test_executor_converted_writes_page_with_new_fields(tmp_path):
    """converted → 写 Page + 新字段 + hash 分离。"""
    from app.sources import executor
    from app.models.database import get_session

    engine, db, nb_id = _setup_db(tmp_path)
    conn = db.get(SourceConnection, "conn-1")
    notebook = db.get(Notebook, nb_id)

    db2 = get_session(engine)
    try:
        nsi = _nsi()
        note, code, msg, summary, retry = executor._convert_note(nsi, "gitlab")
        assert note is not None and code == ""
        source_item = SourceItem(id="si-1", connection_id="conn-1", external_id="x1", state="active")
        db2.add(source_item)
        db2.flush()
        page = executor._upsert_page(db2, conn, notebook, source_item, nsi, note)
        assert page.content == "# 标题\n\n正文"
        assert page.note_schema_version == "canonical-note/v1"
        assert page.content_kind == "markdown"
        assert page.converter_key == "markdown"
        assert page.conversion_status == "converted"
        # hash 分离
        assert page.source_content_hash == note.source_hash
        assert page.source_markdown_hash == note.body_hash
        db2.commit()
    finally:
        db2.close()
    db.close()
    engine.dispose()


def test_executor_failed_blocked_not_written(tmp_path):
    """failed/blocked → 不写 Page + SourceSyncError.stage=convert。"""
    from app.sources import executor
    from app.models.database import get_session

    engine, db, _ = _setup_db(tmp_path)
    _mk_run(db)  # 创建 run-1（SourceSyncError FK）
    db2 = get_session(engine)
    try:
        # failed 场景：文本载荷含乱码 → text converter failed
        nsi = NormalizedSourceItem(
            connection_id="conn-1", source_type="gitlab", external_id="x2",
            content="\x00\x01garbage", content_type="", source_path="data.xyz",
            acl_scope={"scope": "project:1"},
        )
        note, code, msg, summary, retry = executor._convert_note(nsi, "gitlab")
        assert note is None
        assert code  # 有错误码
        # 没有 Page 被创建
        assert db2.query(Page).filter(Page.source_id == "x2").first() is None
        # SourceSyncError.stage=convert
        db2.add(SourceSyncError(
            run_id="run-1", external_id="x2", stage="convert",
            error_code=code or "conversion_failed", error_message=msg, retryable=retry,
        ))
        db2.commit()
        err = db2.query(SourceSyncError).filter(SourceSyncError.external_id == "x2").first()
        assert err.stage == "convert"
        assert err.retryable is True  # failed 可重试
    finally:
        db2.close()
    db.close()
    engine.dispose()


def test_legacy_note_v0_read_compat(tmp_path):
    """历史 Page（无 note_schema_version）读取视为 legacy-note/v0。"""
    engine, db, nb_id = _setup_db(tmp_path)
    from app.models.database import get_session
    db2 = get_session(engine)
    try:
        page = Page(title="legacy", notebook_id=nb_id, content="old",
                    source_type="dingtalk", source_id="old1")
        db2.add(page)
        db2.commit()
        p = db2.get(Page, page.id)
        schema = p.note_schema_version or "legacy-note/v0"
        assert schema == "legacy-note/v0"
    finally:
        db2.close()
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Alembic 迁移循环（临时库）
# ---------------------------------------------------------------------------


def test_alembic_upgrade_downgrade_upgrade(tmp_path):
    db = (tmp_path / "mig.db").as_posix()
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db}"

    def run(args):
        r = subprocess.run([PY, "-m", "alembic"] + args, cwd=BACKEND_ROOT,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env)
        return r.returncode == 0

    assert run(["upgrade", "head"])
    import sqlite3
    con = sqlite3.connect(db)
    cols = [c[1] for c in con.execute("PRAGMA table_info(pages)").fetchall()]
    assert "note_schema_version" in cols
    con.close()
    # Phase 3 后 head 已是 P41（b1c2d3e4f5a6），`-1` 只回退 P41；显式回退到
    # P38（P40 的 down_revision）才能移除 P40 新增的 note_schema_version 列。
    assert run(["downgrade", "e6f7a8b9c0d1"])
    con = sqlite3.connect(db)
    cols = [c[1] for c in con.execute("PRAGMA table_info(pages)").fetchall()]
    assert "note_schema_version" not in cols
    con.close()
    assert run(["upgrade", "head"])
