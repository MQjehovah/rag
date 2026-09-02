"""Phase 2.5：retry_derived 真实 Wiki 调度 + 统计互斥 + 边界封闭 + SourcePayloadError code 格式。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.sources.schemas import InputRepresentation, SourcePayloadError, NormalizedSourceItem


# ---------------------------------------------------------------------------
# 一、retry_derived 真实 schedule_page_refresh changed 断言
# ---------------------------------------------------------------------------


def test_retry_derived_triggers_real_wiki_refresh(tmp_path, monkeypatch):
    """第二轮 retry_derived 成功后，真实 wiki_refresh_scheduler.schedule_page_refresh
    被调用 1 次、page_id 正确、changed is True（monkeypatch 底层调度器）。"""
    import app.sources.registry as reg
    import app.api.pages as pages_mod
    import app.sources.executor as exe_mod
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as wiki_sched
    from app.config import settings
    from tests.test_phase22_execute_run import _register_fake, _FakeGitlabConnector, _nsi, _setup, _mk_run

    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    url = f"sqlite:///{(tmp_path / 'wiki.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)

    calls = {"index": 0, "wiki": []}

    async def fake_index(page_id, title, content, *, schedule_graph=False):
        calls["index"] += 1
        if calls["index"] == 1:
            raise RuntimeError("index boom")
        from app.models.database import Page as _P
        page = db.get(_P, page_id)
        if page is not None:
            page.index_dirty = False
            page.indexed_content_hash = page.current_content_hash
            db.commit()

    def fake_wiki_schedule(page_id, changed=False):
        calls["wiki"].append((page_id, changed))
        return True

    orig_index = pages_mod.background_index_page
    orig_ev = exe_mod.sync_page_evidence
    orig_graph = exe_mod._schedule_graph_rebuild
    orig_wiki = exe_mod._schedule_wiki_refresh_for_page
    orig_wiki_sched = wiki_sched.schedule_page_refresh
    pages_mod.background_index_page = fake_index
    exe_mod.sync_page_evidence = lambda *a, **k: None
    exe_mod._schedule_graph_rebuild = lambda *a, **k: None
    exe_mod._schedule_wiki_refresh_for_page = lambda db, pid, action, changed=False: (
        fake_wiki_schedule(pid, changed=changed)
    )
    # 底层调度器（真实调用点）
    wiki_sched.schedule_page_refresh = fake_wiki_schedule
    try:
        # 第一轮 index 失败
        conn.items["w1"] = lambda: _nsi("# 正文", external_id="w1")
        run = _mk_run(db)
        _run_once = _import_run_once()
        _run_once(db, run, patch_derived=False)
        db.expire_all()
        assert calls["index"] == 1
        assert calls["wiki"] == []  # index 失败不调度 Wiki

        # 第二轮 unchanged + index_dirty → retry 成功
        _run_once(db, _mk_run(db, connection_id="conn-1"), patch_derived=False)
        db.expire_all()
        assert calls["index"] == 2
        assert len(calls["wiki"]) == 1
        page_id, changed = calls["wiki"][0]
        from app.models.database import Page as _P
        page = db.query(_P).filter(_P.source_id == "w1").first()
        assert page_id == page.id
        assert changed is True  # Phase 2.5：retry_derived 必须 changed=True

        # 普通 unchanged（无 index_dirty）不调度 Wiki
        # （第二轮后 index_dirty=False → 第三轮 unchanged 无派生恢复 → wiki 不调用）
        calls["wiki"].clear()
        _run_once(db, _mk_run(db, connection_id="conn-1"), patch_derived=False)
        db.expire_all()
        assert calls["wiki"] == []
    finally:
        pages_mod.background_index_page = orig_index
        exe_mod.sync_page_evidence = orig_ev
        exe_mod._schedule_graph_rebuild = orig_graph
        exe_mod._schedule_wiki_refresh_for_page = orig_wiki
        wiki_sched.schedule_page_refresh = orig_wiki_sched
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))


def _import_run_once():
    from tests.test_phase22_execute_run import _run_once
    return _run_once


# ---------------------------------------------------------------------------
# 二、统计互斥（数据库断言）
# ---------------------------------------------------------------------------


def test_stats_mutual_exclusion_first_failed_second_ok(tmp_path, monkeypatch):
    """第一轮 create + index 失败：Page 存在、created=0、failed=1、run=failed、index_dirty=True；
    第二轮 retry 成功：unchanged=1、failed=0、run=succeeded、Page 不重复、index_dirty=False。"""
    import app.sources.registry as reg
    import app.api.pages as pages_mod
    import app.sources.executor as exe_mod
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as wiki_sched
    from app.config import settings
    from tests.test_phase22_execute_run import _register_fake, _FakeGitlabConnector, _nsi, _setup, _mk_run

    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    url = f"sqlite:///{(tmp_path / 'stats.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)

    index_calls = {"n": 0}

    async def fake_index(page_id, title, content, *, schedule_graph=False):
        index_calls["n"] += 1
        if index_calls["n"] == 1:
            raise RuntimeError("index boom")
        from app.models.database import Page as _P
        page = db.get(_P, page_id)
        if page is not None:
            page.index_dirty = False
            page.indexed_content_hash = page.current_content_hash
            db.commit()

    orig_index = pages_mod.background_index_page
    orig_ev = exe_mod.sync_page_evidence
    orig_graph = exe_mod._schedule_graph_rebuild
    orig_wiki = exe_mod._schedule_wiki_refresh_for_page
    orig_wiki_sched = wiki_sched.schedule_page_refresh
    pages_mod.background_index_page = fake_index
    exe_mod.sync_page_evidence = lambda *a, **k: None
    exe_mod._schedule_graph_rebuild = lambda *a, **k: None
    exe_mod._schedule_wiki_refresh_for_page = lambda db, pid, action, changed=False: True
    wiki_sched.schedule_page_refresh = lambda pid, changed=False: True
    try:
        from tests.test_phase22_execute_run import _run_once
        from app.models.database import Page as _P, SourceSyncRun as _R

        # 第一轮：create + index 失败
        conn.items["s1"] = lambda: _nsi("# 正文", external_id="s1")
        run = _mk_run(db)
        _run_once(db, run, patch_derived=False)
        db.expire_all()
        page = db.query(_P).filter(_P.source_id == "s1").first()
        assert page is not None  # Page 已持久化
        assert page.index_dirty is True
        r = db.get(_R, run.id)
        assert r.created_count == 0
        assert r.updated_count == 0
        assert r.unchanged_count == 0
        assert r.failed_count == 1
        assert r.status == "failed"

        # 第二轮：unchanged + retry 成功
        run2 = _mk_run(db, connection_id="conn-1")
        _run_once(db, run2, patch_derived=False)
        db.expire_all()
        r2 = db.get(_R, run2.id)
        assert r2.unchanged_count == 1
        assert r2.failed_count == 0
        assert r2.status == "succeeded"
        page2 = db.query(_P).filter(_P.source_id == "s1").first()
        assert page2.id == page.id  # 不重复创建
        assert page2.index_dirty is False
    finally:
        pages_mod.background_index_page = orig_index
        exe_mod.sync_page_evidence = orig_ev
        exe_mod._schedule_graph_rebuild = orig_graph
        exe_mod._schedule_wiki_refresh_for_page = orig_wiki
        wiki_sched.schedule_page_refresh = orig_wiki_sched
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))


def test_stats_content_changed_evidence_fail(tmp_path, monkeypatch):
    """content_changed + evidence 失败：updated=0、failed=1。"""
    import app.sources.registry as reg
    import app.api.pages as pages_mod
    import app.sources.executor as exe_mod
    import app.core.knowledge_compiler_v3.wiki_refresh_scheduler as wiki_sched
    from app.config import settings
    from tests.test_phase22_execute_run import _register_fake, _FakeGitlabConnector, _nsi, _setup, _mk_run

    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    url = f"sqlite:///{(tmp_path / 'stats2.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)

    ev_calls = {"n": 0}

    async def fake_index(page_id, title, content, *, schedule_graph=False):
        from app.models.database import Page as _P
        page = db.get(_P, page_id)
        if page is not None:
            page.index_dirty = False
            page.indexed_content_hash = page.current_content_hash
            db.commit()

    def fake_evidence(*a, **k):
        ev_calls["n"] += 1
        if ev_calls["n"] == 2:  # 第一轮成功，第二轮（content_changed）失败
            raise RuntimeError("evidence boom")

    orig_index = pages_mod.background_index_page
    orig_ev = exe_mod.sync_page_evidence
    orig_graph = exe_mod._schedule_graph_rebuild
    orig_wiki = exe_mod._schedule_wiki_refresh_for_page
    orig_wiki_sched = wiki_sched.schedule_page_refresh
    pages_mod.background_index_page = fake_index
    exe_mod.sync_page_evidence = fake_evidence
    exe_mod._schedule_graph_rebuild = lambda *a, **k: None
    exe_mod._schedule_wiki_refresh_for_page = lambda db, pid, action, changed=False: True
    wiki_sched.schedule_page_refresh = lambda pid, changed=False: True
    try:
        from tests.test_phase22_execute_run import _run_once
        from app.models.database import SourceSyncRun as _R

        conn.items["s2"] = lambda: _nsi("# 正文 v1", external_id="s2")
        run = _mk_run(db)
        _run_once(db, run, patch_derived=False)
        db.expire_all()
        r = db.get(_R, run.id)
        # 第一轮 create 成功（index/evidence 都成功）
        assert r.created_count == 1
        assert r.failed_count == 0

        # content_changed + evidence 失败
        conn.items["s2"] = lambda: _nsi("# 正文 v2", external_id="s2", version="v2")
        run2 = _mk_run(db, connection_id="conn-1")
        _run_once(db, run2, patch_derived=False)
        db.expire_all()
        r2 = db.get(_R, run2.id)
        assert r2.updated_count == 0
        assert r2.failed_count == 1
    finally:
        pages_mod.background_index_page = orig_index
        exe_mod.sync_page_evidence = orig_ev
        exe_mod._schedule_graph_rebuild = orig_graph
        exe_mod._schedule_wiki_refresh_for_page = orig_wiki
        wiki_sched.schedule_page_refresh = orig_wiki_sched
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))


# ---------------------------------------------------------------------------
# 三、NormalizedSourceItem 边界对抗
# ---------------------------------------------------------------------------


def test_identity_fields_required_and_stripped():
    for f in ("connection_id", "source_type", "external_id"):
        with pytest.raises(ValueError):
            NormalizedSourceItem(**{"connection_id": "c", "source_type": "t", "external_id": "x",
                                    "content": "hi", f: ""})
        with pytest.raises(ValueError):
            NormalizedSourceItem(**{"connection_id": "c", "source_type": "t", "external_id": "x",
                                    "content": "hi", f: "  "})
        with pytest.raises(ValueError):
            NormalizedSourceItem(**{"connection_id": "c", "source_type": "t", "external_id": "x",
                                    "content": "hi", f: " padded "})


def test_content_type_must_be_str():
    with pytest.raises(ValueError):
        NormalizedSourceItem(connection_id="c", source_type="t", external_id="x",
                             content="hi", content_type=123)  # type: ignore[arg-type]


def test_acl_metadata_must_be_mapping():
    for f in ("acl_scope", "metadata_json"):
        with pytest.raises(ValueError):
            NormalizedSourceItem(**{"connection_id": "c", "source_type": "t", "external_id": "x",
                                    "content": "hi", f: [1, 2]})


def test_attachments_element_must_be_source_attachment():
    from app.sources.schemas import SourceAttachment
    with pytest.raises(ValueError):
        NormalizedSourceItem(connection_id="c", source_type="t", external_id="x",
                             content="hi", attachments=["not-attachment"])
    item = NormalizedSourceItem(connection_id="c", source_type="t", external_id="x",
                                content="hi", attachments=[SourceAttachment(external_id="a")])
    assert len(item.attachments) == 1


def test_preconverted_rejects_bytes():
    with pytest.raises(ValueError):
        NormalizedSourceItem(connection_id="c", source_type="t", external_id="x",
                             content="", content_bytes=b"%PDF",
                             input_representation=InputRepresentation.PRECONVERTED_MARKDOWN)


def test_preconverted_rejects_empty_text():
    with pytest.raises(ValueError):
        NormalizedSourceItem(connection_id="c", source_type="t", external_id="x",
                             content="",
                             input_representation=InputRepresentation.PRECONVERTED_MARKDOWN)


def test_original_auto_computes_source_hash():
    item = NormalizedSourceItem(connection_id="c", source_type="t", external_id="x",
                                content="hello", input_representation=InputRepresentation.ORIGINAL)
    assert item.original_source_hash == hashlib.sha256(b"hello").hexdigest()


def test_deleted_invalid_hash_rejected():
    with pytest.raises(ValueError):
        NormalizedSourceItem(connection_id="c", source_type="t", external_id="x",
                             deleted=True, original_source_hash="not-a-hash")


def test_deleted_empty_identity_rejected():
    with pytest.raises(ValueError):
        NormalizedSourceItem(connection_id="c", source_type="t", external_id="",
                             deleted=True)


# ---------------------------------------------------------------------------
# 四、SourcePayloadError error_code 格式
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [
    "lowercase", "HAS SPACE", "has-dash", "contains/slash", " leading", "trailing ", "",
    "123START", "A b", "aBc",
])
def test_source_payload_error_invalid_code(code):
    with pytest.raises(ValueError):
        SourcePayloadError(code, safe_message="m", retryable=True)


@pytest.mark.parametrize("code", [
    "SOURCE_PATH_UNSAFE", "SOURCE_FILE_MISSING", "MARKDOWN_HASH_MISMATCH", "A1_B2_C",
])
def test_source_payload_error_valid_code(code):
    err = SourcePayloadError(code, safe_message="m", retryable=True)
    assert err.error_code == code
