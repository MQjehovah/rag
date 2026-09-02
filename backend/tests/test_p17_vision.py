"""P17：图片理解、visual_pending 质量门、非多模态降级。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import jwt_utils
from app.core.vision_analysis import (
    analyze_asset,
    enqueue_vision_analysis,
    persist_disabled_placeholders,
    persist_vlm_observations,
    process_vision_analysis_jobs,
    VisionAnalysisResult,
)
from app.main import app
from app.models.database import (
    AssetObservation,
    EvidenceItem,
    Page,
    PageChunk,
    VisionAnalysisJob,
    init_db,
)

ADMIN = {"id": "u1", "username": "admin", "groups": ["__local_admin__"], "is_admin": True}
SAMPLES = json.loads(
    (Path(__file__).parent / "fixtures" / "p17_vision_samples.json").read_text(encoding="utf-8")
)


def _session(engine):
    return sessionmaker(bind=engine)()


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _connection_record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    yield engine
    engine.dispose()


def test_disabled_does_not_invent_function_observation(engine, monkeypatch):
    monkeypatch.setattr("app.config.settings.vision_mode", "disabled")
    db = _session(engine)
    db.add(Page(id="p1", title="截图手册", content="点击启动按钮"))
    db.flush()
    db.add(PageChunk(id="c1", page_id="p1", chunk_index=0, content="## 操作\n点击启动", image_id="img-1"))
    db.commit()
    written = persist_disabled_placeholders(db, "p1")
    db.commit()
    assert written == 1
    obs = db.query(AssetObservation).all()
    assert obs
    assert all(not (row.content or "").strip() for row in obs)
    assert all(row.observation_type != "layout" or not (row.content or "").strip() for row in obs)
    assert obs[0].analysis_status == "skipped"
    assert obs[0].error_category == "vision_disabled"
    db.close()


def test_sensitive_image_not_sent_remote(monkeypatch):
    monkeypatch.setattr("app.config.settings.vision_mode", "remote")
    monkeypatch.setattr("app.config.settings.vision_remote_allowed", True)
    image = Path(__file__).parent / "fixtures" / "p17_sample.jpg"
    image.write_bytes(b"fake")
    called = {"n": 0}

    def _boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("sensitive image must not call provider")

    monkeypatch.setattr("app.core.vision_provider.get_vision_provider", lambda: type("P", (), {"analyze": _boom})())
    result = analyze_asset(image, source_text="身份证号码 110101", title="敏感截图")
    assert result.status == "skipped"
    assert result.error_category == "sensitive_blocked"
    assert called["n"] == 0











def test_ten_acceptance_sample_paths(engine, monkeypatch):
    """10 类验收截图路径：用合成样本 + mock Provider，不连真实 VLM。"""
    assert len(SAMPLES) == 10
    db = _session(engine)
    image = Path(__file__).parent / "fixtures" / "p17_sample.jpg"
    image.write_bytes(b"fake-image")

    for sample in SAMPLES:
        if sample["id"] == "sensitive":
            monkeypatch.setattr("app.config.settings.vision_mode", "remote")
            monkeypatch.setattr("app.config.settings.vision_remote_allowed", True)
            result = analyze_asset(image, source_text=sample["context"], title=sample["title"])
            assert result.error_category == sample["error_category"]
            continue
        if sample["id"] == "disabled_text_only":
            monkeypatch.setattr("app.config.settings.vision_mode", "disabled")
            result = analyze_asset(image, source_text=sample["context"], title=sample["title"])
            assert result.status == "skipped"
            assert result.error_category == "vision_disabled"
            continue

        payload = sample["payload"]

        class _Fake:
            def analyze(self, *_args, **_kwargs):
                if sample.get("raise"):
                    raise TimeoutError("timeout")
                return payload

        monkeypatch.setattr("app.config.settings.vision_mode", "local")
        monkeypatch.setattr("app.core.vision_provider.get_vision_provider", lambda: _Fake())
        result = analyze_asset(image, source_text=sample["context"], title=sample["title"])
        assert result.status == sample["status"]
        assert result.error_category == sample.get("error_category", "")
        if result.status == "done":
            persist_vlm_observations(
                db,
                asset_id=sample["id"],
                page_id="page-samples",
                payload=payload,
                result=result,
            )
            db.commit()
            obs = db.query(AssetObservation).filter(AssetObservation.asset_id == sample["id"]).all()
            assert obs
            if sample.get("inferred"):
                assert any(row.inferred for row in obs)
    db.close()


def test_inferred_observation_serialized(engine):
    db = _session(engine)
    db.add(AssetObservation(
        id="o-inf", asset_id="img", observation_type="ui_function",
        content="可能是启动按钮", inferred=True, analysis_status="done",
    ))
    db.commit()
    from app.core.evidence_query import serialize_observation
    obs = db.query(AssetObservation).first()
    payload = serialize_observation(obs)
    assert payload["inferred"] is True
    db.close()


def test_vision_job_survives_session_and_persists_result(engine, monkeypatch, tmp_path):
    image = tmp_path / "screen.jpg"
    image.write_bytes(b"fake-image")
    db = _session(engine)
    db.add(Page(id="job-page", title="操作界面", content="点击启动"))
    db.flush()
    db.add(PageChunk(
        id="job-chunk", page_id="job-page", chunk_index=0,
        content="点击启动", image_id=str(image),
    ))
    job = enqueue_vision_analysis(db, "job-page")
    db.commit()
    job_id = job.id
    db.close()

    class _Fake:
        def analyze(self, *_args, **_kwargs):
            return {
                "image_type": "ui",
                "summary": "启动按钮用于开始任务",
                "confidence": "high",
                "steps": ["点击启动按钮"],
            }

    monkeypatch.setattr("app.config.settings.vision_mode", "local")
    monkeypatch.setattr("app.core.vision_provider.get_vision_provider", lambda: _Fake())
    worker_db = _session(engine)
    result = process_vision_analysis_jobs(worker_db)
    worker_db.close()

    verify = _session(engine)
    persisted = verify.query(VisionAnalysisJob).filter_by(id=job_id).one()
    assert result == {"jobs": 1, "processed": 1, "failed": 0}
    assert persisted.status == "succeeded"
    assert persisted.attempt == 1
    assert verify.query(AssetObservation).filter_by(
        asset_id=str(image), observation_type="ui_function", analysis_status="done"
    ).count() == 1
    verify.close()


def test_retryable_vision_failure_is_retried(engine, monkeypatch, tmp_path):
    image = tmp_path / "retry.jpg"
    image.write_bytes(b"fake-image")
    db = _session(engine)
    db.add(Page(id="retry-page", title="操作界面", content="点击确认"))
    db.flush()
    db.add(PageChunk(
        id="retry-chunk", page_id="retry-page", chunk_index=0,
        content="点击确认", image_id=str(image),
    ))
    job = enqueue_vision_analysis(db, "retry-page")
    db.commit()

    class _Timeout:
        def analyze(self, *_args, **_kwargs):
            raise TimeoutError("timeout")

    monkeypatch.setattr("app.config.settings.vision_mode", "local")
    monkeypatch.setattr("app.core.vision_provider.get_vision_provider", lambda: _Timeout())
    first = process_vision_analysis_jobs(db)
    db.refresh(job)
    assert first["failed"] == 1
    assert job.status == "failed"
    assert job.attempt == 1

    class _Recovered:
        def analyze(self, *_args, **_kwargs):
            return {
                "image_type": "ui", "summary": "确认按钮提交表单",
                "confidence": "high", "steps": ["点击确认"],
            }

    monkeypatch.setattr("app.core.vision_provider.get_vision_provider", lambda: _Recovered())
    second = process_vision_analysis_jobs(db)
    db.refresh(job)
    assert second["processed"] == 1
    assert job.status == "succeeded"
    assert job.attempt == 2
    db.close()
