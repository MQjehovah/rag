import asyncio
import base64
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List

import httpx

from app.config import settings
from app.core.ingest import _index_imported, _get_or_create_notebook
from app.core.llm import call_llm_json
from app.models.database import (
    Notebook,
    Page,
    SourceItem,
    get_engine,
    get_session,
    init_db,
)

logger = logging.getLogger(__name__)

JIRA_NOTEBOOK = "Jira 知识"
_JIRA_FIELDS = "summary,description,status,resolution,project,issuetype,updated,resolutiondate"

JIRA_AGGREGATE_PROMPT = """你是企业知识库编辑。以下是 Jira 项目中一批已解决的工单，请把内容相关的工单聚合为"主题"，每个主题生成一篇**可复用的故障知识笔记**，而不是每张工单一篇。

工单列表：
{issues}

要求：
1. 按问题主题聚类（如"电机故障""HMI 界面问题""导航异常"）。**尽量合并相关主题**：整批工单最多生成 8 个主题；少于 3 条的主题并入最相近主题或"其他问题"。
2. 每个主题的 Markdown 笔记结构：
   - **标题**精炼（如"电机故障处理合集"）。
   - **## 排查与处理要点**：写成可执行的指南——按"先检查什么 → 常见原因（按频率排序）→ 如何验证"组织。只写工单中实际出现的信息，不要编造；宁可少写，不要泛泛而谈（不要写"均已修复/验证通过"这类无价值语句）。
   - **## 工单明细**：每条按结构化字段列出，只写工单中确实包含的信息，缺失的字段省略（不要写"未记录"凑数）：
     - `- [KEY](Jira链接)：**现象** …；**根因** …；**解决** …`（每项一两句话，精简）
     - 信息量极低（只有"已修复"，无描述无评论）的工单**不单独列条目**，在文末一行带过或忽略。
3. 给出 3-5 个关键词（逗号分隔）。
4. 只返回 JSON，不要其他内容：
{{"groups": [{{"topic": "主题标题", "issue_keys": ["KEY1", "KEY2"], "content": "Markdown 正文", "keywords": "kw1,kw2"}}]}}"""


def _auth_header() -> Dict[str, str]:
    token = base64.b64encode(
        f"{settings.jira_username}:{settings.jira_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def _jira_updated_to_jql(ts: Any) -> str:
    if ts is None:
        days = max(settings.jira_backfill_days, 1)
        ts = datetime.now() - timedelta(days=days)
    return ts.strftime("%Y-%m-%d %H:%M")


def _clean_issue_text(text: str, limit: int = 800) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'!\[[^\]]*\]\([^)]*\)', '[图片]', text)
    cleaned = re.sub(r'\{[^}]*\}', '', cleaned)
    return cleaned.strip()[:limit]


def _issue_summary(issue: Dict[str, Any], comments: List[Dict[str, Any]]) -> str:
    fields = issue.get("fields") or {}
    key = issue.get("key", "")
    title = fields.get("summary") or ""
    desc = _clean_issue_text(fields.get("description") or "", 600)
    parts = [f"[{key}] {title}"]
    if desc:
        parts.append(desc)
    for c in comments[:5]:
        body = _clean_issue_text(c.get("body") or "", 300)
        if body:
            parts.append(f"  评论: {body}")
    return "\n".join(parts)[:1200]


def _fix_links(content: str, issue_keys: List[str]) -> str:
    """Jira links are generated deterministically by the backend; the LLM is
    not trusted to produce correct URLs."""
    base = settings.jira_url.rstrip("/")
    for key in issue_keys:
        pattern = re.compile(rf"\[{re.escape(key)}\](\([^)]*\))")
        content = pattern.sub(f"[{key}]({base}/browse/{key})", content)
    return content


async def _fetch_comments(client: httpx.AsyncClient, key: str) -> List[Dict[str, Any]]:
    try:
        resp = await client.get(
            f"{settings.jira_url.rstrip('/')}/rest/api/2/issue/{key}/comment?maxResults=100"
        )
        resp.raise_for_status()
        return resp.json().get("comments", [])
    except Exception as e:
        logger.warning(f"Jira comments fetch failed for {key}: {e}")
        return []


async def _upsert_aggregate(
    engine,
    notebook_id: str,
    title: str,
    content: str,
    keywords: str,
    issue_keys: List[str],
) -> None:
    db = get_session(engine)
    try:
        page = db.query(Page).filter(
            Page.notebook_id == notebook_id,
            Page.title == title,
        ).first()
        if page is None:
            page = Page(
                id=str(uuid.uuid4()),
                title=title,
                content=content,
                notebook_id=notebook_id,
                keywords=keywords,
            )
            db.add(page)
            db.commit()
            db.refresh(page)
        else:
            page.content = (page.content or "") + "\n\n---\n\n" + content
            if keywords:
                page.keywords = keywords
            db.commit()
        page_id = page.id
        for key in issue_keys:
            row = db.query(SourceItem).filter(
                SourceItem.source_key == "jira",
                SourceItem.item_key == key,
            ).first()
            if row is None:
                db.add(SourceItem(
                    id=str(uuid.uuid4()),
                    source_key="jira",
                    item_key=key,
                    page_id=page_id,
                ))
            else:
                row.page_id = page_id
                row.skipped = False
        db.commit()
    finally:
        db.close()
    try:
        await _index_imported(engine, page_id, title, content, keywords)
    except Exception as e:
        logger.warning(f"Jira aggregate index failed for {title}: {e}")


async def _aggregate_batch(
    engine,
    notebook_id: str,
    project: str,
    batch: List[Dict[str, Any]],
) -> int:
    auth = _auth_header()
    items = []
    async with httpx.AsyncClient(timeout=30.0, headers=auth) as client:
        for issue in batch:
            comments = await _fetch_comments(client, issue.get("key", ""))
            items.append(_issue_summary(issue, comments))
    prompt = JIRA_AGGREGATE_PROMPT.format(issues="\n\n".join(items))
    try:
        result = await call_llm_json(
            [{"role": "user", "content": prompt}],
            context="jira-aggregate",
            timeout=240.0,
        )
    except Exception as e:
        logger.warning(f"Jira aggregate LLM failed: {e}")
        return 0, False
    if not isinstance(result, dict):
        return 0, False
    groups = result.get("groups", []) if isinstance(result, dict) else []
    created = 0
    for g in groups:
        topic = (g.get("topic") or "").strip()
        content = (g.get("content") or "").strip()
        issue_keys = [k for k in (g.get("issue_keys") or []) if isinstance(k, str)]
        if not topic or not content:
            continue
        title = f"{project} · {topic}" if project else topic
        content = _fix_links(content, issue_keys)
        await _upsert_aggregate(engine, notebook_id, title, content, (g.get("keywords") or "").strip(), issue_keys)
        created += 1
    return created, True


async def sync_jira(
    status: Dict[str, Any],
    mode: str = "incremental",
    days: int = 0,
) -> None:
    """Jira sync: resolved issues -> LLM groups them into topic notes
    (aggregation, not one note per ticket).

    mode="incremental": pull only issues updated after the last cursor.
    mode="backfill": ignore the cursor and pull the last ``days``
    (defaults to ``jira_backfill_days``).
    """
    engine = get_engine(settings.database_url)
    init_db(engine)
    status["running"] = True
    status["message"] = "连接 Jira..."
    auth = _auth_header()
    base = settings.jira_url.rstrip("/")

    db = get_session(engine)
    try:
        notebook_id = _get_or_create_notebook(engine, JIRA_NOTEBOOK)
        if mode == "backfill" or days:
            cursor_ts = datetime.now() - timedelta(days=days or settings.jira_backfill_days)
        else:
            row = db.query(SourceItem).filter(SourceItem.source_key == "jira").order_by(
                SourceItem.source_updated.desc()
            ).first()
            cursor_ts = row.source_updated if row else None
    finally:
        db.close()

    projects = [p.strip() for p in settings.jira_projects.split(",") if p.strip()]
    proj_clause = f" AND project in ({', '.join(projects)})" if projects else ""
    jql = (
        f"resolution is not EMPTY AND updated > '{_jira_updated_to_jql(cursor_ts)}'"
        f"{proj_clause} ORDER BY updated ASC"
    )

    issues: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0, headers=auth) as client:
        start_at = 0
        while True:
            try:
                resp = await client.get(
                    f"{base}/rest/api/2/search",
                    params={
                        "jql": jql,
                        "maxResults": 100,
                        "startAt": start_at,
                        "fields": _JIRA_FIELDS,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                status["running"] = False
                status["message"] = f"Jira 查询失败: {e}"
                return
            batch = data.get("issues", [])
            issues.extend(batch)
            start_at += len(batch)
            if start_at >= data.get("total", 0) or not batch:
                break

    status["total"] = len(issues)
    if not issues:
        status["running"] = False
        status["message"] = "没有新的已解决工单"
        return
    status["message"] = f"发现 {len(issues)} 条工单，开始聚合..."

    # group by project
    by_project: Dict[str, List[Dict[str, Any]]] = {}
    for issue in issues:
        project = (issue.get("fields") or {}).get("project") or {}
        by_project.setdefault(project.get("name", ""), []).append(issue)

    total_groups = 0
    processed = 0
    for project, p_issues in by_project.items():
        for i in range(0, len(p_issues), 20):
            chunk = p_issues[i:i + 20]
            n, success = await _aggregate_batch(engine, notebook_id, project, chunk)
            total_groups += n
            processed += len(chunk)
            status["processed"] = processed
            status["message"] = f"Jira 聚合 {processed}/{len(issues)}（已生成 {total_groups} 篇主题笔记）"
            # only advance past issues the LLM actually processed
            if success:
                _record_cursor(engine, chunk)

    status["running"] = False
    status["message"] = f"Jira 同步完成：{len(issues)} 条工单聚合为 {total_groups} 篇主题笔记"
    logger.info(f"Jira sync done: issues={len(issues)} groups={total_groups}")


def _record_cursor(engine, chunk: List[Dict[str, Any]]) -> None:
    """Make sure every processed issue has a source_items row (even if its
    group note failed) so the cursor advances past it."""
    db = get_session(engine)
    try:
        for issue in chunk:
            key = issue.get("key", "")
            if not key:
                continue
            row = db.query(SourceItem).filter(
                SourceItem.source_key == "jira",
                SourceItem.item_key == key,
            ).first()
            if row is None:
                db.add(SourceItem(
                    id=str(uuid.uuid4()),
                    source_key="jira",
                    item_key=key,
                    page_id=None,
                    skipped=True,
                ))
        db.commit()
    finally:
        db.close()
