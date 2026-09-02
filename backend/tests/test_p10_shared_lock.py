"""P10-BE-03：新旧入口共享锁测试。"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app.sources.dingtalk import is_new_sync_running
from app.models.database import SourceConnection, SourceSyncRun, get_engine, init_db


@pytest.fixture()
def lock_db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'lock.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = sessionmaker(bind=engine)()
    db.add(SourceConnection(id="conn1", connector_key="dingtalk", name="钉钉"))
    db.commit()
    db.close()
    engine.dispose()
    return url


def _session(url):
    engine = get_engine(url)
    return sessionmaker(bind=engine)()


def test_no_new_sync_when_none(lock_db):
    db = _session(lock_db)
    assert is_new_sync_running(db) is False
    db.close()


def test_new_sync_detected(lock_db):
    db = _session(lock_db)
    db.add(SourceSyncRun(id=str(uuid.uuid4()), connection_id="conn1", mode="incremental", status="running"))
    db.commit()
    assert is_new_sync_running(db) is True
    db.close()


def test_other_connector_not_detected(lock_db):
    db = _session(lock_db)
    # 非 dingtalk connector 的 running 任务不应被检测
    db.add(SourceConnection(id="conn2", connector_key="gitlab", name="gitlab"))
    db.flush()
    db.add(SourceSyncRun(id=str(uuid.uuid4()), connection_id="conn2", mode="incremental", status="running"))
    db.commit()
    assert is_new_sync_running(db) is False  # 只检测钉钉
    db.close()
