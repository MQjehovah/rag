"""wiki 页面嵌入工具。

wiki 编译是后台流程,嵌入失败不应让整体失败;因此本模块把逐页/逐批的
嵌入失败记入 errors 统计,不向调用方抛异常。
"""
import json
import logging
from typing import Dict, List, Optional

from app.core.rag import EmbeddingService
from app.models.database import WikiPage, get_session

logger = logging.getLogger(__name__)


def _embedding_text(title: str, summary: str) -> str:
    """wiki 页的嵌入文本:标题 + 换行 + 摘要(与设计一致)。"""
    return (title or "") + "\n" + (summary or "")


def _write_embeddings(engine, updates: List[tuple]) -> int:
    """把 (page_id, embedding_json) 批量写回,返回写入行数。"""
    if not updates:
        return 0
    db = get_session(engine)
    try:
        for page_id, payload in updates:
            db.query(WikiPage).filter(WikiPage.id == page_id).update(
                {WikiPage.embedding: payload}
            )
        db.commit()
        return len(updates)
    except Exception:
        logger.warning("wiki 向量写回失败", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        db.close()


async def embed_wiki_pages(
    engine,
    page_ids: Optional[List[str]] = None,
    embedding_svc=None,
    batch_size: int = 32,
) -> Dict[str, int]:
    """为 wiki_pages 计算/刷新向量。

    page_ids 为 None 表示"补齐所有缺失向量的页面";传列表表示只处理这些 id。
    返回 {"embedded": 已写入页数, "errors": 失败页数, "total": 目标页数}。
    嵌入失败只计入 errors,不抛异常(后台编译不应因嵌入失败整体失败)。
    """
    stats = {"embedded": 0, "errors": 0, "total": 0}

    db = get_session(engine)
    try:
        query = db.query(WikiPage.id, WikiPage.title, WikiPage.summary)
        if page_ids is None:
            query = query.filter(WikiPage.embedding.is_(None))
        else:
            ids = list(dict.fromkeys(page_ids))  # 去重,保持首次出现顺序
            if not ids:
                return stats
            query = query.filter(WikiPage.id.in_(ids))
        rows = [
            (r[0], _embedding_text(r[1], r[2]))
            for r in query.all()
        ]
    finally:
        db.close()

    stats["total"] = len(rows)
    if not rows:
        return stats

    svc = embedding_svc or EmbeddingService()
    owns_svc = embedding_svc is None
    try:
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i + batch_size]
            texts = [text for _, text in chunk]
            try:
                vectors = await svc.encode_batch(texts, batch_size=batch_size)
            except Exception:
                logger.warning("wiki 嵌入批次失败", exc_info=True)
                stats["errors"] += len(chunk)
                continue

            updates = []
            for idx, (page_id, _) in enumerate(chunk):
                emb = vectors[idx] if idx < len(vectors or []) else None
                # encode_batch 会吞异常并返回空向量,空向量必须当失败处理,
                # 否则会把无意义的零向量写进库,表现为"搜不到"
                if not emb:
                    stats["errors"] += 1
                    continue
                updates.append((page_id, json.dumps(emb)))
            stats["embedded"] += _write_embeddings(engine, updates)
    finally:
        if owns_svc:
            await svc.close()

    return stats
