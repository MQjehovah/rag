"""Phase J-1 定向测试：钉钉同步链文件夹归属。

覆盖：
- 未映射文件不创建 Page/Chunk/Evidence，SourceItem 记 skipped，run 记录
  FOLDER_NOT_MAPPED 错误（安全原因：路径未配置权限映射）；
- 映射文件正确写入目标 Notebook（忽略连接级默认 Notebook）；
- 子路径继承父映射、更具体路径优先。

全程临时 SQLite，不触碰真实库，不调用真实模型。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    SourcePathMapping,
    Notebook,
    Page,
    PageChunk,
    RuntimeFeatureFlag,
    SourceConnection,
    SourceItem,
    SourceSyncError,
    SourceSyncRun,
    get_engine,
    init_db,
)
from app.sources.executor import execute_run
from app.sources.registry import register
from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange


def _make_dingtalk_connector(docs: dict):
    """docs: external_id -> (dingtalk_path, space_id)"""

    class FakeDingtalkConnector:
        async def iter_changes(self, cursor):
            for doc_id in docs:
                yield SourceChange(external_id=doc_id, external_version="v1")

        async def fetch_acl(self, external_id):
            return SourceACL(scope="space1", raw={"space_id": "space1"})

        async def fetch_item(self, external_id):
            path, space = docs[external_id]
            return NormalizedSourceItem(
                connection_id="conn-1",
                source_type="dingtalk",
                external_id=external_id,
                external_version="v1",
                title=f"{external_id}.pdf",
                content="这是可检索的钉钉正文，包含足够内容。",
                content_hash="",
                source_path=path,
                metadata_json={"space_id": space, "space_name": "知识库"},
                source_updated_at="2026-08-01T00:00:00Z",
            )

    return FakeDingtalkConnector()


@pytest.fixture()
def db(tmp_path):
    engine = get_engine(f"sqlite:///{(tmp_path / 'sync.db').as_posix()}")
    init_db(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture(autouse=True)
def _restore_registry(monkeypatch):
    """备份并恢复 connector registry，避免污染其他测试（test_p10 等）。"""
    from app.sources import registry as _reg
    snapshot = dict(_reg._registry)
    yield
    _reg._registry.clear()
    _reg._registry.update(snapshot)


def _setup(db, notebook_default, mappings):
    db.add(Notebook(id="nb_default", name="默认库", group_id="engineering"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.flush()
    db.add(SourceConnection(
        id="conn", connector_key="dingtalk", name="钉钉知识库", enabled=True,
        target_notebook_id=notebook_default, config_json="{}",
    ))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    for i, (folder_path, target_nb) in enumerate(mappings):
        db.add(SourcePathMapping(
            id=f"m{i}", connection_id="conn", path_namespace="", folder_path=folder_path, notebook_id=target_nb,
        ))
    db.commit()


def _mock_helpers(monkeypatch):
    """Mock 掉远程同步流水线、后台索引、Wiki 调度与债务重验证（不调用真实模型/网络）。"""
    async def _noop(*a, **k):
        return None

    async def _noop_remote(*a, **k):
        return {"new_cursor": {"mode": "incremental"}}

    monkeypatch.setattr(
        "app.sources.executor._run_dingtalk_remote_sync", _noop_remote
    )
    monkeypatch.setattr("app.api.pages.background_index_page", _noop)
    monkeypatch.setattr(
        "app.sources.executor._schedule_wiki_refresh_for_page",
        lambda db, page_id, action, changed=False: None,  # 同步函数，不 await
    )
    monkeypatch.setattr(
        "app.core.retrieval.debt_service.notify_knowledge_changed_for_page",
        lambda db, pid: {"resolved": 0, "checked": 0},
    )
    # 避免 sync_page_evidence 真实计算（Evidence 提取不必要）
    monkeypatch.setattr("app.sources.executor.sync_page_evidence", lambda db, page_id: None)


@pytest.mark.asyncio
async def test_unmapped_file_creates_no_page(db, monkeypatch):
    """未映射文件不创建 Page/Chunk/Evidence，SourceItem 记 skipped + FOLDER_NOT_MAPPED。"""
    _setup(db, "nb_default", [])
    _mock_helpers(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-unmapped": ("未映射目录/文件.pdf", "space1"),
    }))

    await execute_run(db, db.get(SourceSyncRun, "run"))

    run = db.get(SourceSyncRun, "run")
    # 无 Page 创建
    assert db.query(Page).count() == 0
    assert db.query(PageChunk).count() == 0
    # SourceItem 记 skipped + 安全原因
    item = db.query(SourceItem).one()
    assert item.state == "skipped"
    assert item.last_error == "路径未配置权限映射"
    # 同步错误记录 FOLDER_NOT_MAPPED
    err = db.query(SourceSyncError).one()
    assert err.error_code == "SOURCE_PATH_NOT_MAPPED"
    assert err.retryable is False


@pytest.mark.asyncio
async def test_mapped_file_goes_to_target_notebook(db, monkeypatch):
    """映射文件正确写入目标 Notebook（优先于连接级默认 Notebook）。"""
    db.add(Notebook(id="nb_target", name="研发库", group_id="engineering"))
    _setup(db, "nb_default", [("产品资料", "nb_target")])
    _mock_helpers(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-1": ("产品资料/操作手册/换胎.pdf", "space1"),
    }))

    await execute_run(db, db.get(SourceSyncRun, "run"))

    run = db.get(SourceSyncRun, "run")
    assert run.status == "succeeded"
    page = db.query(Page).one()
    assert page.notebook_id == "nb_target"
    assert page.source_id == "doc-1"
    assert page.source_type == "dingtalk"
    item = db.query(SourceItem).one()
    assert item.state == "active"


@pytest.mark.asyncio
async def test_subpath_inherit_and_precedence(db, monkeypatch):
    """父路径继承 + 更具体路径优先。"""
    db.add(Notebook(id="nb_parent", name="父库", group_id="engineering"))
    db.add(Notebook(id="nb_child", name="子库", group_id="sales"))
    db.flush()
    _setup(db, "nb_default", [
        ("产品资料", "nb_parent"),
        ("产品资料/操作手册", "nb_child"),
    ])
    _mock_helpers(monkeypatch)
    register("dingtalk", lambda config: _make_dingtalk_connector({
        "doc-a": ("产品资料/操作手册/换胎.pdf", "space1"),
        "doc-b": ("产品资料/常见问题.pdf", "space1"),
    }))

    await execute_run(db, db.get(SourceSyncRun, "run"))

    pages = {p.source_id: p.notebook_id for p in db.query(Page).all()}
    assert pages["doc-a"] == "nb_child"  # 更具体路径优先
    assert pages["doc-b"] == "nb_parent"  # 父路径继承
