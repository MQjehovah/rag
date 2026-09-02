"""Phase 2.3：explicit pending vs inferred legacy + 严格 converted + original_source_hash 验证 +
fetch 阶段 SourcePayloadError + 派生两轮计数。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.sources.dingtalk import DingTalkConnector
from app.sources.schemas import InputRepresentation, SourcePayloadError
from app.core.dingtalk_storage import DingTalkLocalStorage


def _legacy_md(doc_id: str) -> str:
    return (
        '---\n'
        f'source_type: "dingtalk"\n'
        f'dingtalk_node_id: "{doc_id}"\n'
        'conversion_pipeline_version: "dingtalk-markdown-pipeline-v24"\n'
        'converter: "hybrid_pdf"\n'
        '---\n\n# 正文'
    )


def _mk_storage(tmp_path, doc):
    storage = DingTalkLocalStorage(tmp_path / "dt")
    storage.ensure_directories()
    storage._write_manifest({"last_inventory_at": "2026-01-01", "documents": [doc]})
    return storage


def _md(storage, doc_id, text):
    f = storage.markdown_root / f"{doc_id}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode("utf-8"))
    return f


def _raw(storage, doc_id, data: bytes):
    f = storage.raw_root / f"{doc_id}.pdf"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def _conn(storage):
    return DingTalkConnector({"connection_id": "c1"}, storage=storage)


# ---------------------------------------------------------------------------
# 一、explicit pending vs inferred legacy
# ---------------------------------------------------------------------------


def test_explicit_pending_uses_raw_even_with_legacy_md(tmp_path):
    """显式 pending + 合法旧 Markdown + 新 raw → 必须使用新 raw（忽略 Markdown）。"""
    raw = b"%PDF new raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "pending",  # 显式
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", _legacy_md("d1"))  # 完整合法旧管道 Frontmatter
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw  # 用新 raw
    assert item.metadata_json.get("manifest_is_markdown") is not True
    assert item.input_representation == InputRepresentation.ORIGINAL


def test_inferred_legacy_converted_uses_markdown(tmp_path):
    """inferred legacy converted + 合法旧 Markdown → 使用 Markdown。"""
    md = _legacy_md("d1")
    raw = b"%PDF old raw"
    # 旧 Manifest：只有 status=converted，无 conversion_status
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "status": "converted",  # 旧字段
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    f = _md(storage, "d1", md)
    _raw(storage, "d1", raw)
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.metadata_json.get("manifest_is_markdown") is True  # 用 Markdown
    assert item.input_representation == InputRepresentation.PRECONVERTED_MARKDOWN


def test_pending_without_provenance_uses_raw(tmp_path):
    """pending 无 provenance（conversion_status 显式 pending，非 inferred）+ 合法旧 Markdown + raw → 用 raw。"""
    raw = b"%PDF no-provenance raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "pending",  # 显式（无 inferred 标志）
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", _legacy_md("d1"))
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw


def test_unknown_status_conflict(tmp_path):
    """unknown conversion_status → MANIFEST_STATE_CONFLICT。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "weird_status",
    })
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "MANIFEST_STATE_CONFLICT"
    assert excinfo.value.stage == "fetch"
    assert excinfo.value.retryable is False


# ---------------------------------------------------------------------------
# 二、严格 converted + original_source_hash 验证
# ---------------------------------------------------------------------------


def _converted_storage(tmp_path, *, md_text=None, raw=None, with_md_hash=True, with_raw=True,
                       with_src_hash=True, src_hash=None):
    """构造 converted 场景：手写 manifest（不走 _write_manifest 规范化，消除状态依赖）。"""
    import json
    from app.core.dingtalk_storage import DingTalkLocalStorage as _Storage
    raw = raw if raw is not None else b"%PDF real raw"
    md_text = md_text if md_text is not None else _legacy_md("d1")
    storage = _Storage(tmp_path / "dt")
    storage.ensure_directories()
    f = _md(storage, "d1", md_text)
    _raw(storage, "d1", raw)
    doc = {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf" if with_raw else "",
        "conversion_status": "converted",
        "source_file_hash": src_hash if src_hash else hashlib.sha256(raw).hexdigest() if with_src_hash else "",
        "conversion_status_inferred_from_legacy": False,
    }
    if with_md_hash:
        doc["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    (storage.root / "manifest.json").write_text(
        json.dumps({"last_inventory_at": "2026-01-01", "documents": [doc]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return storage, raw, f


def test_converted_markdown_raw_hash_correct(tmp_path):
    """converted + 正确 raw hash → original_source_hash = source_file_hash。"""
    storage, raw, f = _converted_storage(tmp_path)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.input_representation == InputRepresentation.PRECONVERTED_MARKDOWN
    assert item.original_source_hash == hashlib.sha256(raw).hexdigest()


def test_converted_raw_missing_fail_closed(tmp_path):
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
        "source_file_hash": hashlib.sha256(b"x").hexdigest(),
    })
    f = _md(storage, "d1", _legacy_md("d1"))
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    # raw 文件不存在
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "SOURCE_FILE_MISSING"


def test_converted_raw_hash_missing_fail_closed(tmp_path):
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
        "source_file_hash": "",  # 缺 raw hash
    })
    f = _md(storage, "d1", _legacy_md("d1"))
    _raw(storage, "d1", b"%PDF real")
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "MANIFEST_STATE_CONFLICT"


def test_converted_raw_hash_illegal_fail_closed(tmp_path):
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
        "source_file_hash": "not-a-hash",
    })
    f = _md(storage, "d1", _legacy_md("d1"))
    _raw(storage, "d1", b"%PDF real")
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "SOURCE_HASH_INVALID"


def test_converted_raw_content_mismatch_fail_closed(tmp_path):
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
        "source_file_hash": hashlib.sha256(b"different").hexdigest(),
    })
    f = _md(storage, "d1", _legacy_md("d1"))
    _raw(storage, "d1", b"%PDF real")  # 内容与 hash 不符
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "SOURCE_HASH_MISMATCH"


def test_raw_manifest_no_hash_auto_computes(tmp_path):
    """raw Manifest 无 source_file_hash 但文件存在 → 自动计算 original_source_hash。"""
    raw = b"%PDF no-hash raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "",
        "raw_path": "raw/d1.pdf", "conversion_status": "pending",
        "source_file_hash": "",  # 缺 hash
    })
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.original_source_hash == hashlib.sha256(raw).hexdigest()  # 自动计算


def test_legacy_markdown_hash_by_md_bytes(tmp_path):
    """legacy Markdown 的 markdown_hash 按 md_bytes 校验（UTF-8 BOM）。"""
    md = _legacy_md("d1")
    raw = b"%PDF legacy"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "status": "converted",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    f = _md(storage, "d1", "﻿" + md)  # UTF-8 BOM
    _raw(storage, "d1", raw)
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()  # 按文件 bytes
    storage._write_manifest(manifest)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.metadata_json.get("manifest_is_markdown") is True


# ---------------------------------------------------------------------------
# 三、fetch 阶段 SourcePayloadError（完整 execute_run）
# ---------------------------------------------------------------------------


def test_execute_run_first_item_fetch_payload_error(tmp_path, monkeypatch):
    """第一条即 fetch SourcePayloadError → SourceSyncError.stage=fetch + failed_count=1，无 Page。"""
    import app.sources.registry as reg
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.models.database import (
        Notebook, Page, SourceConnection, SourceItem, SourceSyncError, SourceSyncRun,
        RuntimeFeatureFlag, get_engine, get_session, init_db,
    )
    from app.core.path_mapping import SourcePathMapping
    from app.config import settings

    url = f"sqlite:///{(tmp_path / 'f1.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    nb = Notebook(id="nb1", name="nb", group_id="g1")
    db.add(nb)
    db.flush()
    db.add(SourceConnection(id="conn-1", connector_key="gitlab", name="git", enabled=True,
                            target_notebook_id="nb1", config_json="{}"))
    db.flush()
    db.add(SourcePathMapping(connection_id="conn-1", path_namespace="1", folder_path="",
                             notebook_id="nb1"))
    db.commit()

    class _BoomConn:
        key = "gitlab"

        async def iter_changes(self, cursor):
            yield SourceChange(external_id="boom1", deleted=False, external_version="v1")

        async def fetch_acl(self, eid):
            return SourceACL(scope="1", raw={"project_id": 1}, resolve_failed=False)

        async def fetch_item(self, eid):
            raise SourcePayloadError("SOURCE_FILE_MISSING", stage="fetch", retryable=True,
                                     safe_message="原始文件缺失", internal_detail="detail")

        async def fetch_attachments(self, eid):
            return []

    reg.register("gitlab", lambda c: _BoomConn())
    db.add(SourceSyncRun(id="frun1", connection_id="conn-1", mode="incremental", status="running"))
    db.commit()
    from app.sources.executor import execute_run
    asyncio.run(execute_run(db, db.get(SourceSyncRun, "frun1")))
    db.expire_all()
    # 无 Page
    assert db.query(Page).count() == 0
    # SourceSyncError.stage=fetch + code
    err = db.query(SourceSyncError).filter(SourceSyncError.external_id == "boom1").first()
    assert err is not None and err.stage == "fetch"
    assert err.error_code == "SOURCE_FILE_MISSING"
    assert err.retryable is True
    assert err.error_message == "原始文件缺失"  # safe_message，无绝对路径
    # failed_count=1
    assert db.get(SourceSyncRun, "frun1").failed_count == 1
    db.close()
    engine.dispose()


def test_execute_run_second_item_fetch_error_no_cross_pollution(tmp_path, monkeypatch):
    """第一条成功、第二条 fetch SourcePayloadError → 第一条 Page 不受影响、dirty 未被误标。"""
    import app.sources.registry as reg
    from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange
    from app.models.database import (
        Notebook, Page, SourceConnection, SourceItem, SourceSyncError, SourceSyncRun,
        RuntimeFeatureFlag, get_engine, get_session, init_db,
    )
    from app.core.path_mapping import SourcePathMapping
    from app.config import settings
    import app.api.pages as pages_mod
    import app.sources.executor as exe_mod

    url = f"sqlite:///{(tmp_path / 'f2.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    nb = Notebook(id="nb1", name="nb", group_id="g1")
    db.add(nb)
    db.flush()
    db.add(SourceConnection(id="conn-1", connector_key="gitlab", name="git", enabled=True,
                            target_notebook_id="nb1", config_json="{}"))
    db.flush()
    db.add(SourcePathMapping(connection_id="conn-1", path_namespace="1", folder_path="",
                             notebook_id="nb1"))
    db.commit()

    order = [0]

    class _TwoConn:
        key = "gitlab"

        async def iter_changes(self, cursor):
            yield SourceChange(external_id="ok1", deleted=False, external_version="v1")
            yield SourceChange(external_id="bad2", deleted=False, external_version="v1")

        async def fetch_acl(self, eid):
            return SourceACL(scope="1", raw={"project_id": 1}, resolve_failed=False)

        async def fetch_item(self, eid):
            order[0] += 1
            if eid == "ok1":
                return NormalizedSourceItem(
                    connection_id="conn-1", source_type="gitlab", external_id="ok1",
                    external_version="v1", title="t", content="# 第一条", content_type="md",
                    source_path="docs/a.md", source_url="", source_updated_at="2026-01-01",
                    acl_scope={"scope": "1", "groups": [], "resolve_failed": False, "raw": {}},
                )
            raise SourcePayloadError("SOURCE_HASH_MISMATCH", stage="fetch", retryable=False,
                                     safe_message="原始文件哈希不一致", internal_detail="detail2")

        async def fetch_attachments(self, eid):
            return []

    reg.register("gitlab", lambda c: _TwoConn())
    db.add(SourceSyncRun(id="frun2", connection_id="conn-1", mode="incremental", status="running"))
    db.commit()

    # 隔离派生（focus 转换事务 + fetch 错误捕获）
    orig_index = pages_mod.background_index_page
    orig_ev = exe_mod.sync_page_evidence
    orig_graph = exe_mod._schedule_graph_rebuild
    async def _noop_async(*a, **k):
        pass
    def _noop(*a, **k):
        pass
    pages_mod.background_index_page = _noop_async
    exe_mod.sync_page_evidence = _noop
    exe_mod._schedule_graph_rebuild = _noop
    try:
        asyncio.run(exe_mod.execute_run(db, db.get(SourceSyncRun, "frun2")))
    finally:
        pages_mod.background_index_page = orig_index
        exe_mod.sync_page_evidence = orig_ev
        exe_mod._schedule_graph_rebuild = orig_graph
    db.expire_all()
    # 第一条 Page 正常
    page1 = db.query(Page).filter(Page.source_id == "ok1").first()
    assert page1 is not None
    assert page1.content == "# 第一条"
    assert page1.index_dirty is True  # 派生 noop，index_dirty 保持（未误标 dirty 来自第二条）
    # 第二条 error
    err = db.query(SourceSyncError).filter(SourceSyncError.external_id == "bad2").first()
    assert err is not None and err.stage == "fetch"
    assert err.error_code == "SOURCE_HASH_MISMATCH"
    assert err.retryable is False
    # 没有 UnboundLocalError（execute_run 正常完成）
    db.close()
    engine.dispose()
