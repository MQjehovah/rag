"""P30：bootstrap 幂等 + 钉钉远程流水线 + setup 状态测试。"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models.database import (
    Notebook,
    RuntimeFeatureFlag,
    SourceConnection,
    get_engine,
    init_db,
)
from app.sources.bootstrap import (
    ensure_dingtalk_connection,
    get_connector_setup_status,
)
from app.sources.dingtalk_pipeline import (
    DingTalkRemoteSyncPipeline,
    build_retry_scope,
    map_sync_mode,
)


@pytest.fixture()
def boot_db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'boot.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


def test_bootstrap_idempotent(boot_db, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_app_key", "key")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "secret")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    boot_db.add(Notebook(id="nb", name="钉钉知识库", group_id="group-1"))
    boot_db.commit()

    first = ensure_dingtalk_connection(boot_db)
    second = ensure_dingtalk_connection(boot_db)

    assert first is not None
    assert first.id == second.id  # 幂等，不重复创建
    assert boot_db.query(SourceConnection).filter(
        SourceConnection.connector_key == "dingtalk"
    ).count() == 1


def test_bootstrap_no_credentials_returns_none(boot_db, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_app_key", "")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "")
    boot_db.add(Notebook(id="nb", name="钉钉知识库", group_id="group-1"))
    boot_db.commit()
    assert ensure_dingtalk_connection(boot_db) is None


def test_bootstrap_no_group_notebook_returns_none(boot_db, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_app_key", "key")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "secret")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    # 笔记本没有 group_id → 不能静默选择
    boot_db.add(Notebook(id="nb2", name="钉钉知识库", group_id=None))
    boot_db.commit()
    assert ensure_dingtalk_connection(boot_db) is None


def test_setup_status_reports_blockers(boot_db, monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_app_key", "")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "")
    status = get_connector_setup_status(boot_db)
    dt = status["dingtalk"]
    assert dt["configured"] is False
    codes = [b["code"] for b in dt["setup_blockers"]]
    assert "credentials_missing" in codes
    assert "connection_missing" in codes


def test_map_sync_mode():
    assert map_sync_mode("incremental") == {"complete_snapshot": False, "rescan_all": False}
    assert map_sync_mode("backfill") == {"complete_snapshot": False, "rescan_all": False}
    assert map_sync_mode("full_reconcile") == {"complete_snapshot": True, "rescan_all": True}


def test_build_retry_scope():
    manifest = {
        "documents": [
            {"document_id": "a", "source_status": "active", "download_status": "downloaded", "conversion_status": "converted"},
            {"document_id": "b", "source_status": "active", "download_status": "failed", "conversion_status": "pending"},
            {"document_id": "c", "source_status": "active", "download_status": "downloaded", "conversion_status": "failed"},
            {"document_id": "d", "source_status": "deleted", "download_status": "downloaded", "conversion_status": "converted"},
        ]
    }
    assert set(build_retry_scope(manifest)) == {"b", "c"}
