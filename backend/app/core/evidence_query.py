"""Evidence 查询与序列化（P1-BE-09）。

V3 计划 6.3 P1-BE-09：Evidence API 支持按 Page、Asset、Card 查询和定位。
纯查询/序列化逻辑放此模块（可独立单测），API 端点薄封装。

定位语义（配合 P1-FE-01/02）：
- 文本/表格 Evidence → locator_json（页码/heading/image_id/chunk_index/content_type）
  + 来源 page 标题
- 图片 Observation（asset_id = {file_hash}/{filename}）→ image_url（直接由
  asset_id 构造，与 /api/upload/pdf-pages/ 静态路由一致）
- 表格 Observation（asset_id = chunk_id）→ 关联 page_id

可见性过滤由 API 层注入 visible_page_ids（W13 越权防护），本模块不鉴权。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.database import AssetObservation, EvidenceItem, Page

# 图片 asset_id 形态：{64 位 hex}/{filename}
_IMAGE_ASSET_RE = re.compile(r"^[0-9a-f]{64}/[^/]+$")
IMAGE_URL_PREFIX = "/api/upload/pdf-pages/"


def _parse_locator(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def is_image_asset_id(asset_id: str) -> bool:
    """图片 asset_id 形态（含 64 位 hash 前缀与斜杠），区别于 chunk_id。"""
    return bool(asset_id and _IMAGE_ASSET_RE.match(asset_id))


def image_url_for(asset_id: str) -> str | None:
    """图片 asset_id → 静态图片 URL；非图片 asset 返回 None。"""
    if not is_image_asset_id(asset_id):
        return None
    return f"{IMAGE_URL_PREFIX}{asset_id}"


def serialize_evidence(ev: EvidenceItem, page_title: str = "") -> dict:
    """EvidenceItem → 可定位 dict（含 locator 与来源 page 标题）。"""
    return {
        "id": ev.id,
        "evidence_type": ev.evidence_type,
        "content": ev.content or "",
        "locator": _parse_locator(ev.locator_json),
        "content_hash": ev.content_hash,
        "source_doc_hash": getattr(ev, "source_doc_hash", None),
        "extraction_method": ev.extraction_method,
        "model_name": ev.model_name,
        "confidence": float(ev.confidence) if ev.confidence is not None else None,
        "needs_review": bool(ev.needs_review),
        "status": ev.status,
        "source": {
            "page_id": ev.source_page_id,
            "title": page_title,
            "chunk_id": ev.source_chunk_id,
        },
    }


def serialize_observation(obs: AssetObservation) -> dict:
    """AssetObservation → 可定位 dict（图片带 image_url，table 带 chunk 定位）。"""
    locator: dict = {}
    image_url = image_url_for(obs.asset_id)
    if image_url:
        locator["image_url"] = image_url
    else:
        locator["chunk_id"] = obs.asset_id  # table Observation 的 asset_id 即 chunk_id
    return {
        "id": obs.id,
        "asset_id": obs.asset_id,
        "observation_type": obs.observation_type,
        "content": obs.content or "",
        "extraction_method": obs.extraction_method,
        "model_name": obs.model_name,
        "confidence": float(obs.confidence) if obs.confidence is not None else None,
        "needs_review": bool(obs.needs_review),
        "inferred": bool(getattr(obs, "inferred", False)),
        "analysis_status": getattr(obs, "analysis_status", None) or "done",
        "provider": getattr(obs, "provider", None),
        "error_category": getattr(obs, "error_category", None),
        "created_at": obs.created_at.isoformat() if obs.created_at else None,
        "locator": locator,
    }


def query_by_page(
    db: Session,
    page_id: str,
    visible_page_ids: Optional[set] = None,
) -> list[dict]:
    """按 Page 查询该页全部 Evidence（含来源页标题）。"""
    if visible_page_ids is not None and page_id not in visible_page_ids:
        return []
    page = db.query(Page).filter(Page.id == page_id).first()
    title = page.title if page else ""
    evs = (
        db.query(EvidenceItem)
        .filter(EvidenceItem.source_page_id == page_id)
        .order_by(EvidenceItem.created_at, EvidenceItem.id)
        .all()
    )
    return [serialize_evidence(ev, title) for ev in evs]


def query_by_asset(
    db: Session,
    asset_id: str,
    visible_page_ids: Optional[set] = None,
) -> list[dict]:
    """按 Asset 查询该资产的 Observation（图片的 OCR/功能说明，表格的结构）。

    图片 Observation 无 source_page_id，通过 chunk 的 image_id 关联 page 做
    可见性过滤：asset_id 的 filename 部分 = chunk.image_id。
    """
    if visible_page_ids is not None:
        visible = _asset_visible(db, asset_id, visible_page_ids)
        if not visible:
            return []
    obs = (
        db.query(AssetObservation)
        .filter(AssetObservation.asset_id == asset_id)
        .order_by(AssetObservation.created_at, AssetObservation.id)
        .all()
    )
    return [serialize_observation(o) for o in obs]


def _asset_visible(db: Session, asset_id: str, visible_page_ids: set) -> bool:
    """图片 asset 通过 chunk.image_id 关联 page 判断可见性；table asset 直接按 chunk_id。"""
    from app.models.database import PageChunk

    if is_image_asset_id(asset_id):
        filename = asset_id.rsplit("/", 1)[-1]
        page_ids = {
            p[0]
            for p in db.query(PageChunk.page_id)
            .filter(PageChunk.image_id == filename)
            .distinct()
            .all()
        }
    else:
        page_ids = {
            p[0]
            for p in db.query(PageChunk.page_id)
            .filter(PageChunk.id == asset_id)
            .distinct()
            .all()
        }
    # 无 page 关联（异常数据）→ 交由上层决定；默认放行（不因可见性误杀定位）
    if not page_ids:
        return True
    return bool(page_ids & visible_page_ids)


def get_evidence_detail(
    db: Session,
    evidence_id: str,
    visible_page_ids: Optional[set] = None,
) -> dict | None:
    """单条 Evidence 定位详情：正文 + 来源 + 关联 Observation。"""
    ev = db.query(EvidenceItem).filter(EvidenceItem.id == evidence_id).first()
    if ev is None:
        return None
    if visible_page_ids is not None and ev.source_page_id not in visible_page_ids:
        return None
    page = db.query(Page).filter(Page.id == ev.source_page_id).first()
    detail = serialize_evidence(ev, page.title if page else "")
    # 关联 Observation：按 source_asset_id（图片 filename）或 locator.image_id
    related = _related_observations(db, ev)
    detail["observations"] = related
    return detail


def _related_observations(db: Session, ev: EvidenceItem) -> list[dict]:
    """Evidence 关联的 Observation（图片观察）。

    evidence_items.source_asset_id 目前为空（image Evidence 尚未生成），
    但 locator_json.image_id 可能存有图片 filename——据此反查 ocr Observation。
    """
    locator = _parse_locator(ev.locator_json)
    image_id = locator.get("image_id")
    if not image_id:
        return []
    # asset_observations.asset_id = {file_hash}/{filename}，按 filename 尾部匹配
    obs = (
        db.query(AssetObservation)
        .filter(AssetObservation.asset_id.like(f"%/{image_id}"))
        .all()
    )
    return [serialize_observation(o) for o in obs]


def upsert_manual_observation(db: Session, asset_id: str, content: str) -> dict:
    """创建或更新 manual Observation（P1-FE-04）。

    管理员为图片补充功能说明：observation_type='manual'，extraction_method='manual'。
    幂等键：(asset_id, 'manual')——已有则更新 content，无则新建。
    人工说明视为已确认：confidence=1.0、needs_review=False（图片不再「待人工描述」）。
    """
    content = content or ""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = (
        db.query(AssetObservation)
        .filter(
            AssetObservation.asset_id == asset_id,
            AssetObservation.observation_type == "manual",
        )
        .first()
    )
    if existing is not None:
        existing.content = content
        existing.content_hash = content_hash
        existing.confidence = 1.0
        existing.needs_review = False
        db.commit()
        db.refresh(existing)
        return serialize_observation(existing)

    obs = AssetObservation(
        asset_id=asset_id,
        observation_type="manual",
        content=content,
        extraction_method="manual",
        model_name=None,
        confidence=1.0,
        needs_review=False,
        content_hash=content_hash,
    )
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return serialize_observation(obs)
