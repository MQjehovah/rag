from typing import Any, Dict, Set

from sqlalchemy import or_, select, true
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.core.security import has_permission
from app.core.visibility import notebook_visible_condition, wiki_page_visible_condition
from app.models.database import Notebook, Page


def visible_wiki_filter(current_user):
    """WikiPage 的可见性条件(页面 group 规则 AND 所属空间可见性)。

    语义见 app/core/visibility.py;空间为空(默认空间)视为公共。
    有 `*` 权限(含 __local_admin__ 桥接等效)返回恒真条件(而非 None),
    这样调用方可以直接 filter(),不必记得判空——filter(None) 会退化成 WHERE NULL,静默返回 0 行。
    """
    return wiki_page_visible_condition(current_user)


def page_visible_condition(current_user):
    """Page 可见性 SQL 条件(get_visible_page_ids 的语义, 供列表过滤直接复用)。

    admin(has_permission "*", 含 __local_admin__ 桥接)返回恒真条件;
    其余用户:未归属笔记本的页面视为公共, 归属页要求所属笔记本可见。
    回收站过滤(deleted_at IS NULL)由调用方负责, 见 get_visible_page_ids。
    """
    if has_permission(current_user, "*"):
        return true()
    visible_nb_ids = select(Notebook.id).where(notebook_visible_condition(current_user))
    return or_(Page.notebook_id.is_(None), Page.notebook_id.in_(visible_nb_ids))


def get_visible_page_ids(db: Session, current_user) -> Set[str]:
    """Page ids the user may see (unassigned pages are public). Excludes trash."""
    return set(
        p[0]
        for p in db.query(Page.id)
        .filter(Page.deleted_at.is_(None), page_visible_condition(current_user))
        .all()
    )


def keyword_search(
    db: Session,
    query_kw: set,
    visible_ids: Set[str],
) -> Dict[str, Any]:
    """Keyword match over the pre-computed keywords column."""
    kw_scores: Dict[str, float] = {}
    content_snippets: Dict[str, str] = {}
    if not query_kw:
        return kw_scores, content_snippets

    kw_like_conditions = []
    params = {}
    for i, kw in enumerate(query_kw):
        kw_like_conditions.append(f"keywords LIKE :kw{i}")
        params[f"kw{i}"] = f"%{kw}%"

    if not kw_like_conditions:
        return kw_scores, content_snippets

    where_clause = f"({(' OR '.join(kw_like_conditions))}) AND deleted_at IS NULL"
    if visible_ids:
        placeholders = ",".join([f":vid{i}" for i in range(len(visible_ids))])
        for i, vid in enumerate(visible_ids):
            params[f"vid{i}"] = vid
        where_clause = f"{where_clause} AND id IN ({placeholders})"

    result = db.execute(
        sql_text(f"SELECT id, title, content, keywords FROM pages WHERE {where_clause}"),
        params,
    )
    for row in result.fetchall():
        pid = row[0]
        if pid not in visible_ids:
            continue
        page_kw_str = row[3] or ""
        page_kw = set(page_kw_str.split(",")) if page_kw_str else set()
        overlap = set()
        for qkw in query_kw:
            for pkw in page_kw:
                if qkw in pkw or pkw in qkw:
                    overlap.add(qkw)
                    break
        if overlap:
            kw_score = len(overlap) / max(len(query_kw), 1)
            title_bonus = 0.3 if any(kw in (row[1] or "") for kw in overlap) else 0.0
            kw_scores[pid] = min(kw_score + title_bonus, 1.0)
            content_snippets[pid] = (row[2] or "")[:300]
    return kw_scores, content_snippets
