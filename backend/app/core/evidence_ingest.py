"""Incremental PageChunk -> Evidence conversion used by the live ingest path."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.models.database import EvidenceItem, Page, PageChunk


TYPE_MAP = {"text": "text", "image_caption": "text", "table": "table"}


def _locator(chunk: PageChunk) -> str:
    heading = ""
    for line in (chunk.content or "").splitlines():
        if line.strip().startswith("## "):
            heading = line.strip().lstrip("# ").strip()[:255]
            break
    data: dict = {
        "chunk_index": chunk.chunk_index,
        "content_type": chunk.content_type or "text",
    }
    if chunk.page_number is not None:
        data["page_number"] = chunk.page_number
    if chunk.image_id:
        data["image_id"] = chunk.image_id
    if heading:
        data["heading"] = heading
    return json.dumps(data, ensure_ascii=False)


def sync_page_evidence(db: Session, page_id: str) -> int:
    """Idempotently create/update Evidence for every current chunk of a page."""
    page = db.query(Page).filter(Page.id == page_id).first()
    if page is None:
        return 0
    chunks = db.query(PageChunk).filter(PageChunk.page_id == page_id).all()
    existing = {
        item.source_chunk_id: item
        for item in db.query(EvidenceItem).filter(EvidenceItem.source_page_id == page_id).all()
        if item.source_chunk_id
    }
    changed = 0
    current_chunk_ids: set[str] = set()
    for chunk in chunks:
        current_chunk_ids.add(chunk.id)
        content = chunk.content or ""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        item = existing.get(chunk.id)
        if item is None:
            item = EvidenceItem(id=chunk.id, source_chunk_id=chunk.id)
            db.add(item)
        elif item.content_hash == content_hash and item.status == "active":
            continue
        item.source_page_id = page_id
        item.source_asset_id = chunk.image_id
        item.evidence_type = TYPE_MAP.get(chunk.content_type or "text", "text")
        item.content = content
        item.locator_json = _locator(chunk)
        item.content_hash = content_hash
        item.source_doc_hash = page.current_content_hash
        item.extraction_method = "parser"
        item.confidence = 1.0
        item.needs_review = bool(chunk.image_id and chunk.content_type != "image_caption")
        item.status = "active"
        changed += 1

    for chunk_id, item in existing.items():
        if chunk_id not in current_chunk_ids and item.status == "active":
            item.status = "stale"
            changed += 1
    db.commit()
    try:
        from app.core.vision_analysis import process_page_visuals, schedule_vision_for_page
        process_page_visuals(db, page_id)
        schedule_vision_for_page(page_id)
    except Exception:
        pass
    return changed
