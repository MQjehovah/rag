"""统一数据源独立 Worker 执行链路。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.database import Notebook, Page, RuntimeFeatureFlag, SourceConnection, SourceItem, SourceSyncRun, get_engine, init_db
from app.sources.executor import execute_run
from app.sources.registry import register
from app.sources.schemas import NormalizedSourceItem, SourceACL, SourceChange


class FakeConnector:
    async def iter_changes(self, cursor):
        yield SourceChange(external_id="doc-1", external_version="v1")

    async def fetch_acl(self, external_id):
        return SourceACL(scope="group-1", raw={"group_id": "group-1"})

    async def fetch_item(self, external_id):
        return NormalizedSourceItem(
            connection_id="conn-1",
            source_type="fake",
            external_id=external_id,
            external_version="v1",
            title="试点文档",
            content="这是可检索的正文。",
            content_hash="",
            source_updated_at=datetime.now().isoformat(),
        )


@pytest.mark.asyncio
async def test_executor_consumes_run_and_creates_traceable_page(tmp_path, monkeypatch):
    engine = get_engine(f"sqlite:///{(tmp_path / 'executor.db').as_posix()}")
    init_db(engine)
    db = sessionmaker(bind=engine)()
    db.add(Notebook(id="nb", name="受限知识库", group_id="group-1"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.add(SourceConnection(
        id="conn", connector_key="fake-executor", name="fake", enabled=True,
        target_notebook_id="nb", config_json='{"max_items": 10}',
    ))
    db.add(SourceSyncRun(id="run", connection_id="conn", mode="incremental", status="running"))
    db.commit()
    register("fake-executor", lambda config: FakeConnector())

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr("app.api.pages.background_index_page", _noop)
    await execute_run(db, db.get(SourceSyncRun, "run"))

    run = db.get(SourceSyncRun, "run")
    item = db.query(SourceItem).one()
    page = db.get(Page, item.page_id)
    assert run.status == "succeeded"
    assert run.created_count == 1
    assert item.state == "active"
    assert page is not None
    assert page.notebook_id == "nb"
    assert page.source_id == "doc-1"

    db.close()
    engine.dispose()
