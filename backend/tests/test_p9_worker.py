"""P9-BE-05：数据库 Worker 测试。"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.sources.worker import (
    claim_next_run,
    finish_run,
    heartbeat,
    request_cancel,
    requeue_stale_runs,
    should_stop,
)
from app.models.database import RuntimeFeatureFlag, SourceConnection, SourceSyncRun, get_engine, init_db


@pytest.fixture()
def worker_db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'worker.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    db = sessionmaker(bind=engine)()
    db.add(SourceConnection(id="conn1", connector_key="fake", name="c"))
    db.add(RuntimeFeatureFlag(name="source_hub_enabled", enabled=True))
    db.commit()
    db.close()
    engine.dispose()
    return url


def _session(url):
    engine = get_engine(url)
    return sessionmaker(bind=engine)()


def _make_run(db, status="queued", heartbeat_age_seconds=0):
    run = SourceSyncRun(
        id=str(uuid.uuid4()), connection_id="conn1", mode="incremental", status=status,
    )
    db.add(run)
    db.flush()
    if heartbeat_age_seconds:
        run.heartbeat_at = datetime.now() - timedelta(seconds=heartbeat_age_seconds)
    db.commit()
    return run.id


def test_claim_next_run(worker_db):
    db = _session(worker_db)
    run_id = _make_run(db, "queued")
    claimed = claim_next_run(db)
    assert claimed is not None
    assert claimed.id == run_id
    assert claimed.status == "running"
    assert claimed.started_at is not None
    db.close()


def test_claim_none_when_empty(worker_db):
    db = _session(worker_db)
    assert claim_next_run(db) is None
    db.close()


def test_claim_none_when_source_hub_disabled(worker_db):
    db = _session(worker_db)
    _make_run(db, "queued")
    flag = db.query(RuntimeFeatureFlag).filter(RuntimeFeatureFlag.name == "source_hub_enabled").one()
    flag.enabled = False
    db.commit()
    assert claim_next_run(db) is None
    db.close()


def test_heartbeat_updates(worker_db):
    db = _session(worker_db)
    run_id = _make_run(db, "running")
    heartbeat(db, run_id)
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    assert run.heartbeat_at is not None
    db.close()


def test_requeue_stale_runs(worker_db):
    db = _session(worker_db)
    run_id = _make_run(db, "running", heartbeat_age_seconds=1000)  # 超时
    requeued = requeue_stale_runs(db)
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    assert requeued == 1
    assert run.status == "queued"
    db.close()


def test_requeue_ignores_fresh_runs(worker_db):
    db = _session(worker_db)
    run_id = _make_run(db, "running", heartbeat_age_seconds=1)  # 未超时
    requeued = requeue_stale_runs(db)
    assert requeued == 0
    db.close()


def test_request_cancel_and_should_stop(worker_db):
    db = _session(worker_db)
    run_id = _make_run(db, "running")
    assert request_cancel(db, run_id) is True
    assert should_stop(db, run_id) is True
    db.close()


def test_finish_run(worker_db):
    db = _session(worker_db)
    run_id = _make_run(db, "running")
    finish_run(db, run_id, "succeeded")
    run = db.query(SourceSyncRun).filter(SourceSyncRun.id == run_id).first()
    assert run.status == "succeeded"
    assert run.finished_at is not None
    db.close()
