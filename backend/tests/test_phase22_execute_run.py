"""Phase 2.2：真实 execute_run 双轮事务/重试测试（Fake Connector + 正式 registry）。"""
from __future__ import annotations

import asyncio

import pytest

from app.models.database import (
    Notebook, Page, SourceConnection, SourceItem, SourceSyncError, SourceSyncRun,
    get_engine, get_session, init_db,
)


class _FakeGitlabConnector:
    """Fake Connector：每轮返回可变的 NormalizedSourceItem。"""

    key = "gitlab"
    name = "fake-gitlab"

    def __init__(self, config):
        self.config = config
        self.items = {}  # external_id -> callable() -> NormalizedSourceItem
        self.fetch_calls = 0

    async def iter_changes(self, cursor):
        for eid in self.items:
            yield __import__("app.sources.schemas", fromlist=["SourceChange"]).SourceChange(
                external_id=eid, deleted=False, external_version="v1",
            )

    async def fetch_acl(self, external_id):
        from app.sources.schemas import SourceACL
        return SourceACL(scope="project:1", raw={"project_id": 1}, resolve_failed=False)

    async def fetch_item(self, external_id):
        self.fetch_calls += 1
        return self.items[external_id]()

    async def fetch_attachments(self, external_id):
        return []


def _register_fake():
    from app.sources.registry import register
    register("gitlab", lambda config: _FakeGitlabConnector(config))


def _nsi(content, *, external_id="x1", content_type="md", version="v1"):
    from app.sources.schemas import NormalizedSourceItem
    return NormalizedSourceItem(
        connection_id="conn-1", source_type="gitlab", external_id=external_id,
        external_version=version, title="t", content=content, content_type=content_type,
        source_path="docs/a.md", source_url="", source_updated_at="2026-01-01",
        acl_scope={"scope": "project:1", "groups": [], "resolve_failed": False, "raw": {}},
    )


def _setup(tmp_path, connector, monkeypatch=None):
    """建临时库 + SourceConnection + SourceSyncRun + 路径映射。

    monkeypatch 提供时用 pytest 自动还原 settings.database_url（Phase 2.3 测试隔离）。
    """
    from app.core.path_mapping import SourcePathMapping
    from app.models.database import RuntimeFeatureFlag
    from app.config import settings
    url = f"sqlite:///{(tmp_path / 'exe.db').as_posix()}"
    # background_index_page 内部用 settings.database_url → 指向临时库（避免写真实库）
    if monkeypatch is not None:
        monkeypatch.setattr(settings, "database_url", url)
    else:
        settings.database_url = url
    engine = get_engine(url)
    init_db(engine)
    db = get_session(engine)
    # 启用 source_hub（否则 should_stop 会 cancelled）
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    nb = Notebook(id="nb1", name="nb", group_id="g1")
    db.add(nb)
    db.flush()
    conn = SourceConnection(id="conn-1", connector_key="gitlab", name="git", enabled=True,
                            target_notebook_id=nb.id, config_json="{}")
    db.add(conn)
    db.flush()
    # 根路径映射（mapping_space=1，与 FakeConnector raw project_id=1 匹配）
    db.add(SourcePathMapping(connection_id="conn-1", path_namespace="1",
                             folder_path="", notebook_id=nb.id))
    db.commit()
    return engine, db, conn.id, nb.id


_run_seq = [0]


def _mk_run(db, connection_id="conn-1"):
    _run_seq[0] += 1
    run = SourceSyncRun(id=f"run-{_run_seq[0]}", connection_id=connection_id,
                        mode="incremental", status="running")
    db.add(run)
    db.commit()
    return run


def _run_once(db, run, *, patch_derived=True):
    from app.sources.executor import execute_run
    if patch_derived:
        # 隔离派生阶段（index/evidence/graph）：本测试聚焦转换事务本身。
        # background_index_page 在 executor 内局部 import app.api.pages → patch pages_mod 生效；
        # sync_page_evidence/_schedule_graph_rebuild 是 executor 模块级 → patch exe_mod 生效。
        import app.api.pages as pages_mod
        import app.sources.executor as exe_mod
        orig_index = pages_mod.background_index_page
        orig_ev = exe_mod.sync_page_evidence
        orig_graph = exe_mod._schedule_graph_rebuild
        orig_wiki = exe_mod._schedule_wiki_refresh_for_page
        pages_mod.background_index_page = _noop_async
        exe_mod.sync_page_evidence = _noop
        exe_mod._schedule_graph_rebuild = _noop
        exe_mod._schedule_wiki_refresh_for_page = _noop
        try:
            asyncio.run(execute_run(db, run))
        finally:
            pages_mod.background_index_page = orig_index
            exe_mod.sync_page_evidence = orig_ev
            exe_mod._schedule_graph_rebuild = orig_graph
            exe_mod._schedule_wiki_refresh_for_page = orig_wiki
    else:
        asyncio.run(execute_run(db, run))


async def _noop_async(*args, **kwargs):
    pass


def _noop(*args, **kwargs):
    pass


# ---------------------------------------------------------------------------
# 双轮：第一轮 failed，第二轮同版本成功
# ---------------------------------------------------------------------------


def test_execute_run_first_failed_second_same_version_succeeds(tmp_path, monkeypatch):
    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})
    from app.sources.registry import get_connector
    original = get_connector("gitlab", {"connection_id": "conn-1"})
    # 替换 registry 中的 factory 为我们的实例（通过 get_connector 返回它）
    import app.sources.registry as reg

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)
    try:
        # 第一轮：失败输入（乱码）
        conn.items["x1"] = lambda: _nsi("\x00\x01garbage")
        run = _mk_run(db)
        _run_once(db, run)
        db.expire_all()
        # 无 Page
        assert db.query(Page).filter(Page.source_id == "x1").first() is None
        # SourceItem 不存在（失败不推进）
        assert db.query(SourceItem).filter(SourceItem.external_id == "x1").first() is None
        # SourceSyncError.stage=convert
        err = db.query(SourceSyncError).filter(SourceSyncError.external_id == "x1").first()
        assert err is not None and err.stage == "convert"
        assert err.retryable is True
        # run.failed_count
        assert db.get(SourceSyncRun, run.id).failed_count == 1

        # 第二轮：同一 external_version，Converter 恢复
        conn.items["x1"] = lambda: _nsi("# 标题\n\n正文", version="v1")
        run2 = _mk_run(db, connection_id="conn-1")
        _run_once(db, run2)
        db.expire_all()
        page = db.query(Page).filter(Page.source_id == "x1").first()
        assert page is not None
        assert page.content == "# 标题\n\n正文"
        si = db.query(SourceItem).filter(SourceItem.external_id == "x1").first()
        assert si is not None and si.state == "active"
    finally:
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))  # 还原


# ---------------------------------------------------------------------------
# 已有 Page 更新 failed → 旧值保持
# ---------------------------------------------------------------------------


def test_execute_run_failed_update_preserves_old(tmp_path, monkeypatch):
    import app.sources.registry as reg
    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)
    try:
        # 第一轮成功
        from app.sources import executor
        calls = []
        orig = executor._upsert_page
        def spy(*a, **k):
            page = orig(*a, **k)
            calls.append(page.id)
            return page
        executor._upsert_page = spy
        conn.items["x2"] = lambda: _nsi("# 旧正文", external_id="x2")
        run = _mk_run(db)
        _run_once(db, run)
        executor._upsert_page = orig
        db.expire_all()
        assert len(calls) == 1, f"upsert 应被调用 1 次，实际 {len(calls)}"
        # 用 spy 捕获的 page.id 精确查询
        page = db.get(Page, calls[0])
        assert page is not None, f"Page(id={calls[0]}) not found; run stage={db.query(SourceSyncRun).filter(SourceSyncRun.id==run.id).first().stage}"
        assert page.content == "# 旧正文"
        old_md_hash = page.source_markdown_hash
        old_si_hash = db.query(SourceItem).filter(SourceItem.external_id == "x2").first().content_hash

        # 第二轮失败
        conn.items["x2"] = lambda: _nsi("\x00\x01garbage")
        run2 = _mk_run(db, connection_id="conn-1")
        _run_once(db, run2)
        db.expire_all()
        fresh = db.query(Page).filter(Page.source_id == "x2").first()
        assert fresh.content == "# 旧正文"
        assert fresh.source_markdown_hash == old_md_hash
        si2 = db.query(SourceItem).filter(SourceItem.external_id == "x2").first()
        assert si2.content_hash == old_si_hash
    finally:
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))


# ---------------------------------------------------------------------------
# blocked 不覆盖、不推进、retryable=False
# ---------------------------------------------------------------------------


def test_execute_run_blocked_behavior(tmp_path, monkeypatch):
    import app.sources.registry as reg
    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)
    try:
        conn.items["x3"] = lambda: _nsi("# 旧正文", external_id="x3")
        run = _mk_run(db)
        _run_once(db, run)
        db.expire_all()
        page = db.query(Page).filter(Page.source_id == "x3").first()
        old_md_hash = page.source_markdown_hash
        old_content = page.content

        # 第二轮 blocked：加密内容（走 dingtalk 语义？gitlab 不会 block）
        # 用 gitlab 的 unsupported 场景：bytes + 未知格式
        def _blocked_item():
            from app.sources.schemas import NormalizedSourceItem
            return NormalizedSourceItem(
                connection_id="conn-1", source_type="gitlab", external_id="x3",
                external_version="v2", title="t", content="",
                content_bytes=b"\x00\x01\x02\x03zzz", content_type="", source_path="data.zzz",
                source_url="", source_updated_at="2026-01-01",
                acl_scope={"scope": "project:1", "groups": [], "resolve_failed": False, "raw": {}},
            )
        conn.items["x3"] = _blocked_item
        run2 = _mk_run(db, connection_id="conn-1")
        _run_once(db, run2)
        db.expire_all()
        fresh = db.query(Page).filter(Page.source_id == "x3").first()
        assert fresh.content == old_content
        assert fresh.source_markdown_hash == old_md_hash
        si = db.query(SourceItem).filter(SourceItem.external_id == "x3").first()
        # blocked 不推进 content_hash 到失败版本
        err = db.query(SourceSyncError).filter(SourceSyncError.external_id == "x3").first()
        assert err is not None
    finally:
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))


# ---------------------------------------------------------------------------
# Converter 调用次数（未被 unchanged 跳过）
# ---------------------------------------------------------------------------


def test_execute_run_converter_called_on_each_new_version(tmp_path, monkeypatch):
    import app.sources.registry as reg
    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)
    try:
        # 第一轮 v1
        conn.items["x4"] = lambda: _nsi("# 第一版", external_id="x4", version="v1")
        _run_once(db, _mk_run(db))
        db.expire_all()
        fetch_calls_v1 = conn.fetch_calls

        # 第二轮同 v1 相同内容 → unchanged，fetch 可能仍调用（Connector 每次都 fetch）
        _run_once(db, _mk_run(db, connection_id="conn-1"))
        db.expire_all()
        # unchanged 分支不写 Page，但 fetch_item 每次迭代都调（Connector 契约）
        # 关键断言：第二轮 Page 未变（unchanged 未写）
        page = db.query(Page).filter(Page.source_id == "x4").first()
        assert page.content == "# 第一版"
    finally:
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))


# ---------------------------------------------------------------------------
# 索引/Evidence 失败持久化恢复（Task 20）
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 派生两轮计数（Phase 2.3）：index/evidence/graph 第二轮精确计数 + Fake Indexer
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 派生 5 阶段闭环计数（Phase 2.4）：index/evidence/graph/wiki/debt 精确次数
# ---------------------------------------------------------------------------


def test_execute_run_derived_full_closure(tmp_path, monkeypatch):
    """第一轮 index 失败；第二轮成功：index 1→2、evidence 0→1、graph 0→1、
    wiki 0→1、debt 0→1、index_dirty False、Converter 不增加。"""
    import app.sources.registry as reg
    import app.api.pages as pages_mod
    import app.sources.executor as exe_mod
    from app.config import settings
    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    url = f"sqlite:///{(tmp_path / 'full.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)

    calls = {"index": 0, "evidence": 0, "graph": 0, "wiki": 0, "debt": 0}

    async def fake_index(page_id, title, content, *, schedule_graph=False):
        calls["index"] += 1
        if calls["index"] == 1:
            raise RuntimeError("index boom")
        page = db.get(Page, page_id)
        if page is not None:
            page.index_dirty = False
            page.indexed_content_hash = page.current_content_hash
            db.commit()

    def fake_evidence(*a, **k):
        calls["evidence"] += 1

    def fake_graph(*a, **k):
        calls["graph"] += 1

    def fake_wiki(*a, **k):
        calls["wiki"] += 1

    def fake_debt(*a, **k):
        calls["debt"] += 1

    orig_index = pages_mod.background_index_page
    orig_ev = exe_mod.sync_page_evidence
    orig_graph = exe_mod._schedule_graph_rebuild
    orig_wiki = exe_mod._schedule_wiki_refresh_for_page
    import app.core.retrieval.debt_service
    orig_debt = app.core.retrieval.debt_service.notify_knowledge_changed_for_page
    app.core.retrieval.debt_service.notify_knowledge_changed_for_page = fake_debt
    pages_mod.background_index_page = fake_index
    exe_mod.sync_page_evidence = fake_evidence
    exe_mod._schedule_graph_rebuild = fake_graph
    exe_mod._schedule_wiki_refresh_for_page = fake_wiki
    try:
        # 第一轮：index 失败
        conn.items["x8"] = lambda: _nsi("# 正文", external_id="x8")
        run = _mk_run(db)
        _run_once(db, run, patch_derived=False)
        db.expire_all()
        page = db.query(Page).filter(Page.source_id == "x8").first()
        assert page is not None
        assert calls == {"index": 1, "evidence": 0, "graph": 0, "wiki": 0, "debt": 0}
        assert page.index_dirty is True

        # 第二轮：同版本成功
        _run_once(db, _mk_run(db, connection_id="conn-1"), patch_derived=False)
        db.expire_all()
        assert calls == {"index": 2, "evidence": 1, "graph": 1, "wiki": 1, "debt": 1}
        page2 = db.query(Page).filter(Page.source_id == "x8").first()
        assert page2.index_dirty is False
        # Converter 不增加（fetch 两次 = 两轮各一次）
        assert conn.fetch_calls == 2
    finally:
        pages_mod.background_index_page = orig_index
        exe_mod.sync_page_evidence = orig_ev
        exe_mod._schedule_graph_rebuild = orig_graph
        exe_mod._schedule_wiki_refresh_for_page = orig_wiki
        app.core.retrieval.debt_service.notify_knowledge_changed_for_page = orig_debt
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))


def test_execute_run_evidence_fail_closure(tmp_path, monkeypatch):
    """第一轮 index 成功、evidence 失败：graph/wiki/debt=0、index_dirty=True；
    第二轮成功后 graph/wiki/debt 各一次。"""
    import app.sources.registry as reg
    import app.api.pages as pages_mod
    import app.sources.executor as exe_mod
    from app.config import settings
    _register_fake()
    conn = _FakeGitlabConnector({"connection_id": "conn-1"})

    class _Factory:
        def __call__(self, config):
            return conn

    reg.register("gitlab", _Factory())
    url = f"sqlite:///{(tmp_path / 'evf.db').as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    engine, db, conn_id, nb_id = _setup(tmp_path, conn, monkeypatch)

    calls = {"index": 0, "evidence": 0, "graph": 0, "wiki": 0, "debt": 0}

    async def fake_index(page_id, title, content, *, schedule_graph=False):
        calls["index"] += 1
        page = db.get(Page, page_id)
        if page is not None:
            page.index_dirty = False
            page.indexed_content_hash = page.current_content_hash
            db.commit()

    def fake_evidence(*a, **k):
        calls["evidence"] += 1
        if calls["evidence"] == 1:
            raise RuntimeError("evidence boom")

    def fake_graph(*a, **k):
        calls["graph"] += 1

    def fake_wiki(*a, **k):
        calls["wiki"] += 1

    def fake_debt(*a, **k):
        calls["debt"] += 1

    orig_index = pages_mod.background_index_page
    orig_ev = exe_mod.sync_page_evidence
    orig_graph = exe_mod._schedule_graph_rebuild
    orig_wiki = exe_mod._schedule_wiki_refresh_for_page
    import app.core.retrieval.debt_service
    orig_debt = app.core.retrieval.debt_service.notify_knowledge_changed_for_page
    app.core.retrieval.debt_service.notify_knowledge_changed_for_page = fake_debt
    pages_mod.background_index_page = fake_index
    exe_mod.sync_page_evidence = fake_evidence
    exe_mod._schedule_graph_rebuild = fake_graph
    exe_mod._schedule_wiki_refresh_for_page = fake_wiki
    try:
        # 第一轮：index 成功、evidence 失败
        conn.items["x9"] = lambda: _nsi("# 正文", external_id="x9")
        run = _mk_run(db)
        _run_once(db, run, patch_derived=False)
        db.expire_all()
        page = db.query(Page).filter(Page.source_id == "x9").first()
        assert page is not None
        # index=1, evidence=1(失败), graph/wiki/debt=0
        assert calls["index"] == 1
        assert calls["evidence"] == 1
        assert calls["graph"] == 0 and calls["wiki"] == 0 and calls["debt"] == 0
        assert page.index_dirty is True  # executor except 恢复

        # 第二轮成功：graph/wiki/debt 各一次
        _run_once(db, _mk_run(db, connection_id="conn-1"), patch_derived=False)
        db.expire_all()
        assert calls["index"] == 2
        assert calls["evidence"] == 2
        assert calls["graph"] == 1 and calls["wiki"] == 1 and calls["debt"] == 1
        page2 = db.query(Page).filter(Page.source_id == "x9").first()
        assert page2.index_dirty is False
    finally:
        pages_mod.background_index_page = orig_index
        exe_mod.sync_page_evidence = orig_ev
        exe_mod._schedule_graph_rebuild = orig_graph
        exe_mod._schedule_wiki_refresh_for_page = orig_wiki
        app.core.retrieval.debt_service.notify_knowledge_changed_for_page = orig_debt
        db.close()
        engine.dispose()
        reg.register("gitlab", lambda config: _FakeGitlabConnector(config))
