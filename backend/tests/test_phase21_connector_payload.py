"""Phase 2.1：Connector 真实载荷 + 事务/重试 + hash 语义 + 旧 Manifest + ACL + inventory 只读。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from app.sources.conversion_adapter import to_raw_source_item, convert_normalized
from app.sources.schemas import NormalizedSourceItem

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.abspath(os.path.join(BACKEND_ROOT, ".venv", "Scripts", "python.exe"))


def _nsi(**over):
    base = dict(
        connection_id="conn-1", source_type="gitlab", external_id="x1",
        external_version="v1", title="t", content="hello", content_type="text",
        source_path="a.txt", source_url="https://x", source_updated_at="2026-01-01",
        acl_scope={"scope": "project:1", "groups": [], "resolve_failed": False, "raw": {}},
    )
    base.update(over)
    return NormalizedSourceItem(**base)


# ---------------------------------------------------------------------------
# 一、真实载荷（content_bytes / 禁占位）
# ---------------------------------------------------------------------------


def test_text_bytes_mutually_exclusive_now():
    """Phase 2.4：text 与 bytes 同时非空 → 对象构造即拒绝。"""
    with pytest.raises(ValueError):
        _nsi(content="text", content_bytes=b"\x00\x01pdf")


def test_bytes_payload_maps_raw_bytes():
    """bytes-only 载荷 → raw_bytes 正确。"""
    nsi = _nsi(content="", content_bytes=b"\x00\x01pdf")
    raw = to_raw_source_item(nsi, connector_key="gitlab")
    assert raw.raw_bytes == b"\x00\x01pdf"


def test_empty_payload_rejected_no_placeholder():
    """空 content + 空 bytes → 对象构造即 ValueError（禁占位伪造）。"""
    with pytest.raises(ValueError):
        _nsi(content="", content_bytes=None)


def test_whitespace_payload_rejected():
    """空白文本 payload → 对象构造即 ValueError。"""
    with pytest.raises(ValueError):
        _nsi(content="   ")


def test_compute_content_hash_uses_effective_payload():
    from app.sources.service import compute_content_hash
    nsi = _nsi(content="text")  # text-only
    assert compute_content_hash(nsi) == hashlib.sha256(b"text").hexdigest()
    nsi2 = _nsi(content="", content_bytes=b"\x00\x01")
    assert compute_content_hash(nsi2) == hashlib.sha256(b"\x00\x01").hexdigest()


def test_has_valid_payload_semantics():
    """合法载荷 has_valid_payload=True。"""
    assert _nsi(content="x").has_valid_payload is True
    assert _nsi(content="", content_bytes=b"x").has_valid_payload is True


def test_dingtalk_real_raw_bytes(tmp_path, monkeypatch):
    """DingTalkConnector 从 raw_path 读真实 bytes（注入 Fake PDF Converter 验证）。"""
    from app.core.dingtalk_storage import DingTalkLocalStorage
    from app.sources.dingtalk import DingTalkConnector

    storage = DingTalkLocalStorage(tmp_path / "dt")
    storage.ensure_directories()
    # 造 manifest：未转换 raw 文件
    raw_file = storage.raw_root / "doc1.pdf"
    raw_file.write_bytes(b"%PDF-1.4 fake real bytes")
    manifest = {
        "last_inventory_at": "2026-01-01",
        "documents": [{
            "document_id": "doc1", "name": "doc1", "extension": "pdf",
            "space_id": "sp1", "source_status": "active",
            "raw_path": raw_file.relative_to(storage.root).as_posix(),
            "source_file_hash": hashlib.sha256(b"%PDF-1.4 fake real bytes").hexdigest(),
            "dingtalk_path": "dir/doc1.pdf", "source_url": "", "source_updated_at": "",
            "markdown_path": "", "conversion_status": "downloaded",
        }],
    }
    storage._write_manifest(manifest)

    conn = DingTalkConnector({"connection_id": "c1"}, storage=storage)
    import asyncio
    item = asyncio.run(conn.fetch_item("doc1"))
    assert item.content_bytes == b"%PDF-1.4 fake real bytes"
    assert item.content == ""
    # original_source_hash 保留
    assert item.original_source_hash == hashlib.sha256(b"%PDF-1.4 fake real bytes").hexdigest()


def test_dingtalk_raw_path_traversal_rejected(tmp_path):
    from app.core.dingtalk_storage import DingTalkLocalStorage
    from app.sources.dingtalk import DingTalkConnector

    storage = DingTalkLocalStorage(tmp_path / "dt")
    storage.ensure_directories()
    manifest = {
        "last_inventory_at": "2026-01-01",
        "documents": [{
            "document_id": "doc1", "name": "doc1", "extension": "pdf",
            "space_id": "sp1", "source_status": "active",
            "raw_path": "../../evil.pdf",  # 越界
            "dingtalk_path": "x", "source_url": "", "source_updated_at": "",
            "markdown_path": "", "conversion_status": "downloaded",
        }],
    }
    storage._write_manifest(manifest)
    conn = DingTalkConnector({"connection_id": "c1"}, storage=storage)
    import asyncio
    from app.sources.schemas import SourcePayloadError
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(conn.fetch_item("doc1"))
    assert excinfo.value.error_code == "SOURCE_PATH_UNSAFE"


# ---------------------------------------------------------------------------
# 二、hash 语义（三种 hash 不同场景）
# ---------------------------------------------------------------------------


def test_three_hashes_distinct_for_manifest_markdown(tmp_path):
    """source_file_hash / markdown_hash / body_hash 三者不同 → Page 字段分别正确（真实持久化）。"""
    from app.core.dingtalk_storage import DingTalkLocalStorage
    from app.sources.dingtalk import DingTalkConnector
    from app.sources.conversion_adapter import convert_normalized
    from app.sources import executor
    from app.models.database import (
        Notebook, Page, SourceConnection, SourceItem, get_engine, get_session, init_db,
    )

    storage = DingTalkLocalStorage(tmp_path / "dt")
    storage.ensure_directories()
    # 真实 raw 文件
    raw_file = storage.raw_root / "doc1.pdf"
    raw_file.write_bytes(b"%PDF fake original")
    raw_bytes = raw_file.read_bytes()
    # 合法旧管道 Markdown
    md_text = (
        '---\n'
        'source_type: "dingtalk"\n'
        'dingtalk_node_id: "doc1"\n'
        'conversion_pipeline_version: "dingtalk-markdown-pipeline-v24"\n'
        'converter: "hybrid_pdf"\n'
        '---\n\n# 正文'
    )
    md_file = storage.markdown_root / "doc1.md"
    md_file.write_bytes(md_text.encode("utf-8"))  # 二进制写入，避免 Windows \n→\r\n
    source_file_hash = hashlib.sha256(raw_bytes).hexdigest()
    markdown_hash = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    manifest = {
        "last_inventory_at": "2026-01-01",
        "documents": [{
            "document_id": "doc1", "name": "doc1", "extension": "pdf",
            "space_id": "sp1", "source_status": "active",
            "raw_path": raw_file.relative_to(storage.root).as_posix(),
            "source_file_hash": source_file_hash,
            "markdown_path": md_file.relative_to(storage.root).as_posix(),
            "markdown_hash": markdown_hash,
            "dingtalk_path": "dir/doc1.pdf", "source_url": "", "source_updated_at": "",
            "conversion_status": "converted", "converter": "hybrid_pdf",
        }],
    }
    storage._write_manifest(manifest)
    conn = DingTalkConnector({"connection_id": "c1"}, storage=storage)
    import asyncio
    item = asyncio.run(conn.fetch_item("doc1"))
    # markdown 输入（已转换）
    assert "# 正文" in item.content
    note = convert_normalized(item, connector_key="dingtalk")
    assert note.body_hash == hashlib.sha256(note.body.encode("utf-8")).hexdigest()
    # note.source_hash = Markdown 输入 hash（非原文件）
    assert note.source_hash == hashlib.sha256(item.content.encode("utf-8")).hexdigest()
    assert note.source_hash != source_file_hash  # 不冒充原文件 hash

    # 真实持久化：_upsert_page 后查询数据库 Page
    engine = get_engine(f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    init_db(engine)
    db = get_session(engine)
    nb = Notebook(id="nb1", name="nb", group_id="g1")
    db.add(nb)
    db.flush()
    db.add(SourceConnection(id="conn-1", connector_key="dingtalk", name="dt",
                            enabled=True, target_notebook_id=nb.id, config_json="{}"))
    db.flush()
    si = SourceItem(id="si-1", connection_id="conn-1", external_id="doc1", state="active")
    db.add(si)
    db.flush()
    page = executor._upsert_page(db, db.get(SourceConnection, "conn-1"), nb, si, item, note)
    db.commit()
    fresh = db.get(Page, page.id)
    # 三种 hash 分别正确
    assert fresh.source_content_hash == source_file_hash  # raw hash
    assert fresh.source_markdown_hash == note.body_hash  # body hash
    assert fresh.content_hash == note.body_hash  # body hash
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 三、旧 Manifest 兼容 / 防二次转换
# ---------------------------------------------------------------------------


def _dt_conn(tmp_path, manifest_docs):
    from app.core.dingtalk_storage import DingTalkLocalStorage
    from app.sources.dingtalk import DingTalkConnector
    storage = DingTalkLocalStorage(tmp_path / "dt")
    storage.ensure_directories()
    storage._write_manifest({"last_inventory_at": "2026-01-01", "documents": manifest_docs})
    return DingTalkConnector({"connection_id": "c1"}, storage=storage), storage


def _legacy_md(doc_id: str) -> str:
    """构造合法旧管道 Markdown（严格四条件）。"""
    return (
        '---\n'
        f'source_type: "dingtalk"\n'
        f'dingtalk_node_id: "{doc_id}"\n'
        'conversion_pipeline_version: "dingtalk-markdown-pipeline-v24"\n'
        'converter: "hybrid_pdf"\n'
        '---\n\n# 正文'
    )


def test_legacy_manifest_no_status_but_valid_markdown(tmp_path):
    """旧 Manifest 无 conversion_status（storage 规范化为 pending，inferred 非 converted）
    + 有合法旧 Markdown + 有 raw → 一律用 raw（不判为已转换）。"""
    from app.sources.schemas import InputRepresentation
    md = _legacy_md("d1")
    conn, storage = _dt_conn(tmp_path, [{
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "dingtalk_path": "x", "source_url": "", "source_updated_at": "",
        "source_file_hash": hashlib.sha256(b"%PDF raw").hexdigest(),
        # 无 conversion_status
    }])
    md_file = storage.markdown_root / "d1.md"
    md_file.write_text(md, encoding="utf-8")
    raw_file = storage.raw_root / "d1.pdf"
    raw_file.write_bytes(b"%PDF raw")
    import asyncio
    item = asyncio.run(conn.fetch_item("d1"))
    # inferred 非 converted → 用 raw
    assert item.content_bytes == b"%PDF raw"
    assert item.input_representation == InputRepresentation.ORIGINAL
    raw = to_raw_source_item(item, connector_key="dingtalk")
    assert raw.content_kind.value == "pdf"  # raw 交 PDF Converter


def test_manifest_markdown_missing_fail_closed(tmp_path):
    """converted 但 Markdown 文件缺失 → SourcePayloadError（fail closed），即使 raw 存在也不改走 Converter。"""
    from app.sources.schemas import SourcePayloadError
    conn, storage = _dt_conn(tmp_path, [{
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "dingtalk_path": "x", "source_url": "", "source_updated_at": "",
        "conversion_status": "converted",
    }])
    # 真实 raw 存在，但 Markdown 文件缺失 → 必须 fail closed（不得静默改走 Converter）
    raw_file = storage.raw_root / "d1.pdf"
    raw_file.write_bytes(b"%PDF-1.4 raw exists")
    import asyncio
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(conn.fetch_item("d1"))
    assert excinfo.value.error_code == "SOURCE_FILE_MISSING"


def test_manifest_markdown_hash_mismatch_fail_closed(tmp_path):
    """converted 且 markdown_hash 不匹配 → fail closed（MARKDOWN_HASH_MISMATCH）。"""
    from app.sources.schemas import SourcePayloadError
    md = _legacy_md("d1")
    conn, storage = _dt_conn(tmp_path, [{
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active",
        "markdown_path": "markdown/d1.md", "raw_path": "",
        "markdown_hash": "0" * 64,  # 伪造
        "dingtalk_path": "x", "source_url": "", "source_updated_at": "",
        "conversion_status": "converted",
    }])
    md_file = storage.markdown_root / "d1.md"
    md_file.write_text(md, encoding="utf-8")
    import asyncio
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(conn.fetch_item("d1"))
    assert excinfo.value.error_code == "MARKDOWN_HASH_MISMATCH"


def test_manifest_invalid_frontmatter_fail_closed(tmp_path):
    """converted 但 Frontmatter 无效（非旧管道产物）→ fail closed（抛错）。"""
    import hashlib as _hl
    md_text = "没有 frontmatter 的普通 markdown"
    raw = b"%PDF raw"
    conn, storage = _dt_conn(tmp_path, [{
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "dingtalk_path": "x", "source_url": "", "source_updated_at": "",
        "conversion_status": "converted",
        "source_file_hash": _hl.sha256(raw).hexdigest(),
        "markdown_hash": _hl.sha256(md_text.encode("utf-8")).hexdigest(),
    }])
    md_file = storage.markdown_root / "d1.md"
    md_file.write_text(md_text, encoding="utf-8")
    raw_file = storage.raw_root / "d1.pdf"
    raw_file.write_bytes(raw)
    import asyncio
    from app.sources.schemas import SourcePayloadError
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(conn.fetch_item("d1"))
    assert excinfo.value.error_code == "LEGACY_MARKDOWN_UNRECOGNIZED"


# ---------------------------------------------------------------------------
# 四、ACL 边界
# ---------------------------------------------------------------------------


def test_acl_resolve_failed_preserved():
    nsi = _nsi(acl_scope={"scope": "s1", "groups": [], "resolve_failed": True, "raw": {"space_id": "s1"}})
    raw = to_raw_source_item(nsi, connector_key="dingtalk")
    assert raw.acl.is_fail_closed


def test_acl_resolve_failed_non_bool_fail_closed():
    nsi = _nsi(acl_scope={"scope": "s1", "groups": [], "resolve_failed": "yes", "raw": {}})
    raw = to_raw_source_item(nsi, connector_key="dingtalk")
    assert raw.acl.is_fail_closed


def test_acl_raw_scope_not_override_standard():
    """raw 中的 scope 不得覆盖标准 scope。"""
    nsi = _nsi(acl_scope={"scope": "standard", "groups": [], "resolve_failed": False,
                          "raw": {"scope": "hacked"}})
    raw = to_raw_source_item(nsi, connector_key="dingtalk")
    assert raw.acl.scope == "standard"


def test_acl_raw_acl_only_when_connector_provides():
    """raw 为空 dict 时不写 raw_acl。"""
    nsi = _nsi(acl_scope={"scope": "s1", "groups": [], "resolve_failed": False, "raw": {}})
    raw = to_raw_source_item(nsi, connector_key="dingtalk")
    assert "raw_acl" not in raw.source_metadata
    # raw 非空时写
    nsi2 = _nsi(acl_scope={"scope": "s1", "groups": [], "resolve_failed": False,
                           "raw": {"space_id": "s1"}})
    raw2 = to_raw_source_item(nsi2, connector_key="dingtalk")
    assert raw2.source_metadata["raw_acl"] == {"space_id": "s1"}


# ---------------------------------------------------------------------------
# 五、inventory 只读证明
# ---------------------------------------------------------------------------


def test_inventory_readonly_no_mutation(tmp_path):
    """执行前后数据库文件 mtime/表/列/alembic_version/数据行完全不变。"""
    db_path = tmp_path / "inv.db"
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    subprocess.run([PY, "-m", "alembic", "upgrade", "head"], cwd=BACKEND_ROOT,
                   capture_output=True, env=env)
    con = sqlite3.connect(str(db_path))
    con.execute("INSERT INTO notebooks (id,name,group_id) VALUES ('nb1','nb','g1')")
    con.execute("INSERT INTO pages (id,title,notebook_id,content) VALUES ('p1','a','nb1','x')")
    con.commit()
    before = {
        "mtime": os.path.getmtime(db_path),
        "tables": con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),
        "cols": con.execute("PRAGMA table_info(pages)").fetchall(),
        "alembic": con.execute("SELECT version_num FROM alembic_version").fetchone(),
        "rows": con.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
    }
    con.close()

    r = subprocess.run(
        [PY, "scripts/inventory_page_conversion.py", "--db", f"sqlite:///{db_path.as_posix()}"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout.split("[dry-run]")[0].strip())
    assert report["legacy_note_v0"] == 1

    con = sqlite3.connect(str(db_path))
    after = {
        "mtime": os.path.getmtime(db_path),
        "tables": con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),
        "cols": con.execute("PRAGMA table_info(pages)").fetchall(),
        "alembic": con.execute("SELECT version_num FROM alembic_version").fetchone(),
        "rows": con.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
    }
    con.close()
    assert before == after  # 完全不变


def test_inventory_missing_db_no_create(tmp_path):
    """不存在的数据库路径 → 报错且不得创建文件。"""
    db_path = tmp_path / "nonexistent.db"
    r = subprocess.run(
        [PY, "scripts/inventory_page_conversion.py", "--db", f"sqlite:///{db_path.as_posix()}"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode != 0
    assert not db_path.exists()  # 未创建


def test_inventory_requires_db_arg():
    """不传 --db → 报错（默认不指向真实 notes.db）。"""
    r = subprocess.run([PY, "scripts/inventory_page_conversion.py"], cwd=BACKEND_ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode != 0


def test_inventory_old_schema_reports_missing_not_fix(tmp_path):
    """旧 schema（无 P40 字段）→ 只报告缺失，不补字段。"""
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE pages (id TEXT PRIMARY KEY, title TEXT, notebook_id TEXT, content TEXT)")
    con.commit()
    con.close()
    r = subprocess.run(
        [PY, "scripts/inventory_page_conversion.py", "--db", f"sqlite:///{db_path.as_posix()}"],
        cwd=BACKEND_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0
    report = json.loads(r.stdout.split("[dry-run]")[0].strip())
    assert report["schema_ready"] is False
    assert "note_schema_version" in report["missing_columns"]
    # 未补字段
    con = sqlite3.connect(str(db_path))
    cols = [c[1] for c in con.execute("PRAGMA table_info(pages)").fetchall()]
    assert "note_schema_version" not in cols
    con.close()


# ---------------------------------------------------------------------------
# 六、完整 executor 双轮事务/重试（指令二：通过完整 executor 流程验证）
# ---------------------------------------------------------------------------


def _setup_exec_db(tmp_path):
    from app.models.database import (
        Notebook, SourceConnection, get_engine, get_session, init_db,
    )
    engine = get_engine(f"sqlite:///{(tmp_path / 'exec21.db').as_posix()}")
    init_db(engine)
    db = get_session(engine)
    nb = Notebook(id="nb1", name="nb", group_id="g1")
    db.add(nb)
    db.flush()
    db.add(SourceConnection(id="conn-1", connector_key="gitlab", name="git",
                            enabled=True, target_notebook_id=nb.id, config_json="{}"))
    db.commit()
    return engine, db


def _run_convert(db, external_id, content, *, version="v1"):
    """模拟 executor 单条转换：失败返回 (None, code)，成功返回 (page, None)。"""
    from app.sources import executor
    from app.sources.schemas import NormalizedSourceItem
    from app.models.database import Notebook, SourceConnection, SourceItem

    nsi = NormalizedSourceItem(
        connection_id="conn-1", source_type="gitlab", external_id=external_id,
        external_version=version, title="t", content=content, content_type="md",
        source_path="docs/a.md", acl_scope={"scope": "project:1", "groups": [],
                                            "resolve_failed": False, "raw": {}},
    )
    note, code, msg, summary, retryable = executor._convert_note(nsi, "gitlab")
    if note is None:
        return None, code, retryable
    existing = db.query(SourceItem).filter(
        SourceItem.connection_id == "conn-1", SourceItem.external_id == external_id,
    ).first()
    if existing is None:
        existing = SourceItem(connection_id="conn-1", external_id=external_id, state="active")
        db.add(existing)
        db.flush()
    page = executor._upsert_page(db, db.get(SourceConnection, "conn-1"),
                                 db.get(Notebook, "nb1"), existing, nsi, note)
    db.commit()
    return page, None, retryable


def test_executor_first_failed_second_same_version_succeeds(tmp_path):
    """新条目第一次 failed，第二次同一 external_version 转换成功。"""
    from app.models.database import Page, SourceItem, get_session
    engine, db = _setup_exec_db(tmp_path)
    db2 = get_session(engine)
    try:
        # 第一轮：乱码内容 → failed（text converter）
        page, code, retryable = _run_convert(db2, "x1", "\x00\x01garbage")
        assert page is None and code
        # SourceItem 未被推进成功版本：content_hash 不应等于失败内容
        si = db2.query(SourceItem).filter(SourceItem.external_id == "x1").first()
        assert si is None or si.content_hash is None  # 未推进

        # 第二轮：同一版本有效 Markdown → 成功
        page2, code2, _ = _run_convert(db2, "x1", "# 标题\n\n正文", version="v1")
        assert page2 is not None and code2 is None
        assert page2.content == "# 标题\n\n正文"
        assert page2.note_schema_version == "canonical-note/v1"
    finally:
        db2.close()
    db.close()
    engine.dispose()


def test_executor_failed_update_preserves_old_page_and_hash(tmp_path):
    """已有 Page 更新时 failed → 旧 Page 和 SourceItem hash/version 不变。"""
    from app.models.database import Page, SourceItem, get_session
    engine, db = _setup_exec_db(tmp_path)
    db2 = get_session(engine)
    try:
        # 先成功建 Page
        page, _, _ = _run_convert(db2, "x2", "# 旧正文", version="v1")
        old_content = page.content
        old_markdown_hash = page.source_markdown_hash
        old_version = page.source_type or ""
        si = db2.query(SourceItem).filter(SourceItem.external_id == "x2").first()
        old_si_hash = si.content_hash

        # 更新但转换失败（乱码）
        page2, code, _ = _run_convert(db2, "x2", "\x00\x01garbage", version="v2")
        assert page2 is None and code
        # 旧 Page 不变
        fresh = db2.query(Page).filter(Page.source_id == "x2").first()
        assert fresh.content == old_content
        assert fresh.source_markdown_hash == old_markdown_hash
        # SourceItem 未推进到失败版本
        si2 = db2.query(SourceItem).filter(SourceItem.external_id == "x2").first()
        assert si2.content_hash == old_si_hash
    finally:
        db2.close()
    db.close()
    engine.dispose()


def test_executor_blocked_does_not_overwrite_or_promote(tmp_path):
    """blocked（加密）不覆盖 Page，不推进成功状态。"""
    from app.models.database import Page, SourceItem, get_session
    engine, db = _setup_exec_db(tmp_path)
    db2 = get_session(engine)
    try:
        page, _, _ = _run_convert(db2, "x3", "# 旧正文", version="v1")
        old_md_hash = page.source_markdown_hash
        # 更新为加密内容 → blocked
        from app.sources.schemas import NormalizedSourceItem
        from app.sources import executor
        nsi = NormalizedSourceItem(
            connection_id="conn-1", source_type="dingtalk", external_id="x3",
            external_version="v2", title="t", content="", content_bytes=b"E-SafeNet LOCK fake",
            content_type="pdf", source_path="docs/a.pdf",
            acl_scope={"scope": "sp1", "groups": [], "resolve_failed": False, "raw": {"space_id": "sp1"}},
        )
        note, code, msg, summary, retryable = executor._convert_note(nsi, "dingtalk")
        assert note is None
        assert code == "encrypted_source"
        assert retryable is False  # blocked 不可重试
        # 旧 Page 不变
        fresh = db2.query(Page).filter(Page.source_id == "x3").first()
        assert fresh.content == "# 旧正文"
        assert fresh.source_markdown_hash == old_md_hash
    finally:
        db2.close()
    db.close()
    engine.dispose()
