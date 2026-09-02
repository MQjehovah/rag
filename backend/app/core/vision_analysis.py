"""P17：视觉分析编排（与文本 LLM/Embedding/Reranker 解耦）。

VisionProvider.analyze(asset, context) 产出：
status / observations[] / confidence / provider / model / version / error_category。

模式：
- disabled：只占位 + 可选 OCR，不编造图片功能
- local：调用本地/已配置视觉服务
- remote：仅当 vision_remote_allowed 且非敏感图时发送
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.core.vision_provider import get_vision_mode
from app.models.database import AssetObservation, Page, PageChunk, VisionAnalysisJob

logger = logging.getLogger(__name__)

FUNCTION_TYPES = {"ui_function", "operation_flow", "manual"}
_SENSITIVE_RE = re.compile(
    r"(身份证|护照|银行卡|密码|口令|secret|confidential|内部机密|手机号)",
    re.IGNORECASE,
)
_VISUAL_CLAIM_RE = re.compile(r"(点击|按钮|界面|截图|弹窗|图标|菜单|开关)")


@dataclass
class VisionAnalysisResult:
    status: str
    observations: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    provider: str = "disabled"
    model: str = ""
    model_version: str = ""
    error_category: str = ""
    inferred: bool = False
    context_hash: str = ""


def get_vision_mode() -> str:
    from app.core.vision_provider import get_vision_mode as _mode
    return _mode()


def context_hash(*, asset_id: str, title: str, heading: str, ocr: str, source_hash: str) -> str:
    raw = "|".join([asset_id or "", title or "", heading or "", ocr or "", source_hash or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_sensitive_context(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text or ""))


def claim_depends_on_visual(statement: str) -> bool:
    return bool(_VISUAL_CLAIM_RE.search(statement or ""))


def _upsert_observation(
    db: Session,
    *,
    asset_id: str,
    observation_type: str,
    content: str,
    page_id: str | None,
    result: VisionAnalysisResult,
    needs_review: bool,
    confidence: float,
    extraction_method: str,
) -> AssetObservation:
    row = (
        db.query(AssetObservation)
        .filter(
            AssetObservation.asset_id == asset_id,
            AssetObservation.observation_type == observation_type,
        )
        .first()
    )
    if row is None:
        row = AssetObservation(asset_id=asset_id, observation_type=observation_type)
        db.add(row)
    row.content = content
    row.source_page_id = page_id
    row.analysis_status = result.status
    row.provider = result.provider
    row.model_name = result.model or None
    row.model_version = result.model_version or None
    row.error_category = result.error_category or None
    row.context_hash = result.context_hash
    row.inferred = bool(result.inferred)
    row.needs_review = needs_review
    row.confidence = confidence
    row.extraction_method = extraction_method
    row.content_hash = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
    return row


def persist_visual_placeholders(
    db: Session,
    page_id: str,
    *,
    status: str,
    provider: str,
    error_category: str,
) -> int:
    """为图片资产写占位 Observation，不编造 ui_function/layout 内容。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if page is None:
        return 0
    chunks = db.query(PageChunk).filter(
        PageChunk.page_id == page_id,
        PageChunk.image_id.isnot(None),
    ).all()
    current_assets = {chunk.image_id for chunk in chunks if chunk.image_id}
    written = 0
    title = page.title or ""
    source_hash = page.current_content_hash
    for chunk in chunks:
        asset_id = chunk.image_id
        if not asset_id:
            continue
        heading = ""
        for line in (chunk.content or "").splitlines():
            if line.strip().startswith("## "):
                heading = line.strip().lstrip("# ").strip()
                break
        ctx = context_hash(
            asset_id=asset_id, title=title, heading=heading,
            ocr=chunk.content or "", source_hash=source_hash,
        )
        existing = db.query(AssetObservation).filter(
            AssetObservation.asset_id == asset_id,
            AssetObservation.observation_type == "ui_function",
        ).first()
        if existing and existing.context_hash == ctx and existing.analysis_status == "done":
            continue
        sensitive = is_sensitive_context(f"{title}\n{chunk.content or ''}")
        result = VisionAnalysisResult(
            status="skipped" if sensitive else status,
            provider=provider,
            error_category="sensitive_blocked" if sensitive else error_category,
            context_hash=ctx,
            inferred=False,
        )
        _upsert_observation(
            db,
            asset_id=asset_id,
            observation_type="ui_function",
            content="",
            page_id=page_id,
            result=result,
            needs_review=True,
            confidence=0.0,
            extraction_method="none",
        )
        written += 1

    if current_assets:
        stale = db.query(AssetObservation).filter(
            AssetObservation.source_page_id == page_id,
            ~AssetObservation.asset_id.in_(current_assets),
        ).all()
    else:
        stale = db.query(AssetObservation).filter(
            AssetObservation.source_page_id == page_id,
        ).all()
    for row in stale:
        row.analysis_status = "failed"
        row.error_category = "stale_asset"
        row.needs_review = True
    return written


def persist_disabled_placeholders(db: Session, page_id: str) -> int:
    """Vision disabled：为图片资产写 skipped 占位，不编造功能。"""
    return persist_visual_placeholders(
        db, page_id, status="skipped", provider="disabled", error_category="vision_disabled",
    )


def persist_vlm_observations(
    db: Session,
    *,
    asset_id: str,
    page_id: str,
    payload: dict[str, Any],
    result: VisionAnalysisResult,
) -> None:
    """把 VLM JSON 落到 Observation，推断性内容标记 inferred。"""
    conf_map = {"high": 0.9, "medium": 0.7, "low": 0.4}
    confidence = conf_map.get(str(payload.get("confidence") or "").lower(), result.confidence or 0.5)
    inferred = bool(payload.get("inferred")) or str(payload.get("confidence") or "").lower() == "low"
    result.inferred = inferred
    result.status = "done"
    summary = str(payload.get("summary") or "").strip()
    image_type = str(payload.get("image_type") or "")
    if summary:
        _upsert_observation(
            db, asset_id=asset_id, observation_type="ui_function",
            content=json.dumps({"image_type": image_type, "summary": summary}, ensure_ascii=False),
            page_id=page_id, result=result, needs_review=confidence < 0.5,
            confidence=confidence, extraction_method="vlm",
        )
    steps = payload.get("steps") or []
    if steps:
        _upsert_observation(
            db, asset_id=asset_id, observation_type="operation_flow",
            content=json.dumps(steps, ensure_ascii=False),
            page_id=page_id, result=result, needs_review=False,
            confidence=confidence, extraction_method="vlm",
        )
    layout = []
    layout.extend(payload.get("spatial_relations") or [])
    layout.extend(payload.get("callouts") or [])
    if layout and result.provider != "ocr":
        _upsert_observation(
            db, asset_id=asset_id, observation_type="layout",
            content=json.dumps(layout, ensure_ascii=False),
            page_id=page_id, result=result, needs_review=False,
            confidence=confidence, extraction_method="vlm",
        )


def analyze_asset(
    image_path: Path | None,
    *,
    page_number: int = 0,
    source_text: str = "",
    title: str = "",
) -> VisionAnalysisResult:
    """同步分析单张图。敏感图不发远程；disabled 不编造功能。"""
    mode = get_vision_mode()
    ctx = context_hash(
        asset_id=str(image_path or ""), title=title, heading="",
        ocr=source_text, source_hash="",
    )
    combined = f"{title}\n{source_text}"
    if is_sensitive_context(combined) and mode == "remote":
        return VisionAnalysisResult(
            status="skipped", provider="disabled", error_category="sensitive_blocked",
            context_hash=ctx,
        )
    if mode == "disabled" or image_path is None or not Path(image_path).is_file():
        return VisionAnalysisResult(
            status="skipped", provider="disabled", error_category="vision_disabled",
            context_hash=ctx,
        )
    from app.core.vision_provider import get_vision_provider
    provider = get_vision_provider()
    try:
        payload = provider.analyze(Path(image_path), page_number, source_text)
    except Exception as exc:
        logger.warning(f"vision analyze failed: {exc}")
        category = "timeout" if "timeout" in str(exc).lower() else "vision_error"
        return VisionAnalysisResult(
            status="failed", provider=mode, error_category=category,
            context_hash=ctx,
        )
    if not payload:
        return VisionAnalysisResult(
            status="failed", provider=mode, error_category="low_ocr",
            context_hash=ctx,
        )
    image_type = str(payload.get("image_type") or "").lower()
    if image_type in {"blur", "blurry", "low_res"} or payload.get("error_category") == "blur":
        return VisionAnalysisResult(
            status="failed", provider=mode, error_category="blur",
            context_hash=ctx, observations=[payload],
        )
    inferred = (
        bool(payload.get("inferred"))
        or str(payload.get("confidence") or "").lower() == "low"
    )
    return VisionAnalysisResult(
        status="done",
        observations=[payload],
        confidence={"high": 0.9, "medium": 0.7, "low": 0.4}.get(
            str(payload.get("confidence") or "").lower(), 0.5
        ),
        provider=mode,
        model=settings.pdf_vision_model if mode != "disabled" else "",
        inferred=inferred,
        context_hash=ctx,
    )


def process_page_visuals(db: Session, page_id: str) -> int:
    """同步占位；不在上传请求里等待 VLM。"""
    mode = get_vision_mode()
    if mode == "disabled":
        written = persist_disabled_placeholders(db, page_id)
    else:
        written = persist_visual_placeholders(
            db, page_id, status="pending", provider=mode, error_category="",
        )
    db.commit()
    return written


def _vision_job_key(db: Session, page_id: str) -> str:
    page = db.query(Page).filter(Page.id == page_id).first()
    chunks = db.query(PageChunk).filter(
        PageChunk.page_id == page_id,
        PageChunk.image_id.isnot(None),
    ).order_by(PageChunk.chunk_index, PageChunk.id).all()
    raw = json.dumps({
        "page_id": page_id,
        "source_hash": page.current_content_hash if page else None,
        "assets": [
            [chunk.image_id, hashlib.sha256((chunk.content or "").encode("utf-8")).hexdigest()]
            for chunk in chunks
        ],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enqueue_vision_analysis(
    db: Session,
    page_id: str,
    *,
    trigger: str = "ingest",
) -> VisionAnalysisJob | None:
    """按页面内容幂等入队；没有图片的页面不创建空任务。"""
    page = db.query(Page).filter(Page.id == page_id).first()
    if page is None:
        return None
    has_asset = db.query(PageChunk.id).filter(
        PageChunk.page_id == page_id,
        PageChunk.image_id.isnot(None),
    ).first()
    if has_asset is None:
        return None
    key = _vision_job_key(db, page_id)
    existing = db.query(VisionAnalysisJob).filter(
        VisionAnalysisJob.idempotency_key == key,
    ).first()
    if existing is not None:
        return existing
    job = VisionAnalysisJob(
        id=str(uuid.uuid4()),
        page_id=page_id,
        status="queued",
        trigger=trigger,
        idempotency_key=key,
        attempt=0,
        max_attempts=3,
    )
    db.add(job)
    db.flush()
    return job


def _resolve_asset_path(asset_id: str | None) -> Path | None:
    if not asset_id:
        return None
    raw = Path(asset_id)
    if raw.is_file():
        return raw
    candidate = Path(settings.pdf_image_storage_dir) / raw
    return candidate if candidate.is_file() else None


def _analyze_page_assets(db: Session, page_id: str) -> dict[str, Any]:
    """执行一个页面任务并持久化逐图结果。"""
    placeholders = process_page_visuals(db, page_id)
    mode = get_vision_mode()
    if mode == "disabled":
        return {
            "assets": placeholders,
            "done": 0,
            "skipped": placeholders,
            "retryable_failed": 0,
            "review_required": placeholders,
        }

    page = db.query(Page).filter(Page.id == page_id).first()
    if page is None:
        raise ValueError(f"Page 不存在: {page_id}")
    chunks = db.query(PageChunk).filter(
        PageChunk.page_id == page_id,
        PageChunk.image_id.isnot(None),
    ).all()
    summary = {
        "assets": len(chunks),
        "done": 0,
        "skipped": 0,
        "retryable_failed": 0,
        "review_required": 0,
    }
    retryable = {"timeout", "vision_error", "asset_missing"}
    for chunk in chunks:
        asset_id = chunk.image_id
        path = _resolve_asset_path(asset_id)
        if path is None:
            result = VisionAnalysisResult(
                status="failed",
                provider=mode,
                error_category="asset_missing",
                context_hash=context_hash(
                    asset_id=asset_id or "",
                    title=page.title or "",
                    heading="",
                    ocr=chunk.content or "",
                    source_hash=page.current_content_hash or "",
                ),
            )
        else:
            result = analyze_asset(
                path,
                page_number=chunk.page_number or 0,
                source_text=chunk.content or "",
                title=page.title or "",
            )
        if result.status == "done" and result.observations:
            persist_vlm_observations(
                db,
                asset_id=asset_id,
                page_id=page_id,
                payload=result.observations[0],
                result=result,
            )
            summary["done"] += 1
        else:
            result.provider = result.provider or mode
            _upsert_observation(
                db,
                asset_id=asset_id,
                observation_type="ui_function",
                content="",
                page_id=page_id,
                result=result,
                needs_review=True,
                confidence=0.0,
                extraction_method="none",
            )
            summary["review_required"] += 1
            if result.status == "skipped":
                summary["skipped"] += 1
            elif result.error_category in retryable:
                summary["retryable_failed"] += 1
    db.commit()
    return summary


def process_vision_analysis_jobs(db: Session, *, limit: int = 10) -> dict[str, int]:
    """处理持久任务。可恢复错误最多自动尝试 max_attempts 次。"""
    jobs = db.query(VisionAnalysisJob).filter(
        (VisionAnalysisJob.status == "queued")
        | (
            (VisionAnalysisJob.status == "failed")
            & (VisionAnalysisJob.attempt < VisionAnalysisJob.max_attempts)
            & (VisionAnalysisJob.error_category == "retryable_asset_failure")
        )
    ).order_by(VisionAnalysisJob.created_at).limit(limit).all()
    processed = 0
    failed = 0
    for job in jobs:
        job.status = "running"
        job.started_at = datetime.now()
        job.finished_at = None
        job.attempt = (job.attempt or 0) + 1
        db.commit()
        try:
            result = _analyze_page_assets(db, job.page_id)
            job.result_json = json.dumps(result, ensure_ascii=False)
            if result["retryable_failed"]:
                job.status = "failed"
                job.error_category = "retryable_asset_failure"
                job.error_message = f"{result['retryable_failed']} 张图片可重试失败"
                failed += 1
            else:
                job.status = "succeeded"
                job.error_category = None
                job.error_message = None
                processed += 1
        except Exception as exc:
            logger.exception("vision job %s failed", job.id)
            db.rollback()
            job = db.query(VisionAnalysisJob).filter(VisionAnalysisJob.id == job.id).one()
            job.status = "failed"
            job.error_category = "worker_error"
            job.error_message = str(exc)
            failed += 1
        job.finished_at = datetime.now()
        db.commit()
    return {"jobs": len(jobs), "processed": processed, "failed": failed}


def retry_vision_job(db: Session, job_id: str) -> VisionAnalysisJob:
    job = db.query(VisionAnalysisJob).filter(VisionAnalysisJob.id == job_id).first()
    if job is None:
        raise ValueError("视觉任务不存在")
    if job.status == "running":
        raise ValueError("视觉任务正在执行")
    if (job.attempt or 0) >= (job.max_attempts or 3):
        job.max_attempts = (job.attempt or 0) + 1
    job.status = "queued"
    job.error_category = None
    job.error_message = None
    job.finished_at = None
    db.commit()
    return job


def schedule_vision_for_page(page_id: str) -> str | None:
    """只写持久任务，不启动随进程消失的 daemon 线程。"""
    if get_vision_mode() == "disabled":
        return None
    from app.models.database import get_engine, get_session

    engine = get_engine(settings.database_url)
    db = get_session(engine)
    try:
        job = enqueue_vision_analysis(db, page_id, trigger="ingest")
        db.commit()
        return job.id if job is not None else None
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        engine.dispose()


def observation_is_reliable(obs: AssetObservation) -> bool:
    status = obs.analysis_status or "done"
    if status in {"pending", "failed", "skipped"}:
        return False
    if obs.needs_review:
        return False
    if (obs.confidence or 0) < 0.5:
        return False
    return (obs.observation_type or "") in FUNCTION_TYPES
