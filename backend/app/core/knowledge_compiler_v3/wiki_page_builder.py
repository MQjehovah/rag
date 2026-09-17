"""Page 驱动的 Wiki 构建（V4 Phase C 第二次补漏）。

将 Wiki 重构为「原始 Page/PageChunk → 按权限域分析主题 → 创建或增量更新 Wiki
→ 自动成为当前有效知识 → 授权编辑者可直接修改」。

本版本关键能力：
- Page 级持久化待处理状态（Page.wiki_dirty / wiki_compiled_content_hash /
  wiki_last_error），进程重启后可恢复未完成任务。
- 真正的 Page→Wiki 来源关系协调（reconcile_page_wiki_membership），处理
  主题迁移、not_worthy 解除、权限域迁移。
- LLM 调用期间不持有数据库事务：快照 → 结束事务 → LLM → 重新查询对比 → 应用。
- 单 Page 多 ops 在同一事务边界；不同 Page 独立提交。
- 删除来源：唯一来源 → archived；多来源 → 移除 + dirty。

硬约束：构建输入只来自 Page / PageChunk / 标题 / 正文 / Metadata / 已有 Wiki
目录，不得读取 KnowledgeCard / KnowledgeCommunity / KnowledgeClaim / CardBlock。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Callable

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core import access_control
from app.core.access_control import AccessScope
from app.core.knowledge_compiler_v3 import versioning
from app.core.wiki_workspace.routing import ensure_notebook_workspace, page_workspace_id
from app.models.database import (
    Notebook,
    Page,
    PageChunk,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiVersionSource,
)

logger = logging.getLogger(__name__)


class LLMServiceUnavailable(Exception):
    """LLM 服务不可用（未配置 / 401 / 403 / 500 / 网络失败 / 超时）。"""


MAX_TOPICS_PER_PAGE = 2
MIN_CONTENT_CHARS = 10
CONTEXT_CHAR_LIMIT = 6000
_INDEX_SUMMARY_LEN = 60
DEFAULT_CATEGORY = "未分类"

WIKI_INGEST_PROMPT = """你是企业知识库 Wiki 编辑。知识库由多篇原始文档蒸馏而来，你负责把新文档的信息整合进 Wiki。

当前 Wiki 页面索引（标题 | 分类 | 摘要）：
{index}

新文档：
标题：{title}
内容：
{content}

规则：
1. 先判断文档内容是否有价值：垃圾、重复或信息量极低 → worthy=false。
2. 有价值（worthy=true）时：
   - 能归入现有主题则用 update；
   - 全新主题用 create 新建；
   - 每篇文档最多 create/update 共 2 个主题，聚焦核心知识，不罗列流水账。
3. 分类从以下选择或自拟简洁分类：产品资料、操作指南、故障排查、开发技术、部署运维、业务流程。
4. 只返回 JSON，不要其他内容，且必须显式包含 worthy 字段：
{{"worthy": true, "ops": [{{"action": "create", "title": "...", "category": "..."}}, {{"action": "update", "title": "现有页面标题", "category": "..."}}]}}
无价值时返回：
{{"worthy": false, "ops": []}}
"""

WIKI_SYNTHESIS_PROMPT = """你是企业知识库 Wiki 编辑。请根据以下来源文档合成一个 Wiki 主题页的完整 Markdown 正文。

主题标题：{title}
分类：{category}

现有正文（可能为空，含人工润色段落，务必保留仍有效内容）：
{existing}

来源文档：
{sources}

规则：
1. 合并所有来源的知识，不要只保留单一来源，不要用最后一篇覆盖前面。
2. 保留现有正文中仍然有效的内容（含人工润色/修正段落）。
3. 正文用 Markdown；页面间引用用 [[页面标题]] 语法；保留关键命令/代码；内容具体可执行。
4. 给 30 字以内的摘要。
5. 只返回 JSON，不要其他内容：
{{"summary": "...", "content": "..."}}
"""

WIKI_BATCH_SUMMARY_PROMPT = """你是企业知识库 Wiki 编辑。请把以下一批来源文档的要点浓缩成中间摘要，供后续合并用。

主题标题：{title}

来源文档：
{sources}

规则：
1. 保留每份来源的核心知识与关键命令/代码，不要丢弃任何来源。
2. 用简洁 Markdown 条目表达。
3. 只返回 JSON：
{{"summary": "..."}}
"""

WIKI_MAPREDUCE_PROMPT = """你是企业知识库 Wiki 编辑。请根据以下各批来源的中间摘要合成一个 Wiki 主题页的完整 Markdown 正文。

主题标题：{title}
分类：{category}

现有正文（可能为空，含人工润色段落，务必保留仍有效内容）：
{existing}

各批摘要：
{summaries}

规则：
1. 合并所有批次的知识，不丢失任何批次。
2. 保留现有正文中仍然有效的内容（含人工润色/修正段落）。
3. 正文用 Markdown；页面间引用用 [[页面标题]] 语法；保留关键命令/代码。
4. 给 30 字以内的摘要。
5. 只返回 JSON：
{{"summary": "...", "content": "..."}}
"""

# V4 Phase I：版本感知合成提示词。版本标签由确定性识别给出（LLM 不得新增版本，
# 不得把 external_version/提交号当产品版本）。通用说明只提取多个版本都明确共同
# 的内容；版本相关参数/路径/命令必须留在对应版本块，不得错误提升为通用。
WIKI_SYNTHESIS_VERSIONED_PROMPT = """你是企业知识库 Wiki 编辑。请根据以下来源文档合成一个 Wiki 主题页的**分版本** Markdown 正文。

主题标题：{title}
分类：{category}

现有通用说明（可能为空，含人工润色，务必保留仍有效内容）：
{existing_common}

现有各版本正文（含人工保护内容，务必原样保留其仍有效部分）：
{existing_versions}

来源文档（已按产品版本确定性分组，你不得新增/修改版本标签）：
{sources}

规则：
1. **通用说明**：只放入多个版本都明确共同的内容（如安装前关闭服务）。版本相关的参数、路径、命令一律不得写入通用说明。
2. **版本块**：为下方 versions 里给出的每个版本标签分别产出正文；只合并该版本自己的来源，不得把不同版本的配置混成一个不标版本的模糊说明。
3. 保留现有通用说明与各版本正文中仍然有效的内容（含人工润色/修正段落）。
4. 正文用 Markdown；保留关键命令/代码；内容具体可执行。
5. 若同一版本内部来源互相矛盾，不得伪造统一结论，在 diff_notice 里写「该版本信息存在差异」。
6. 给 30 字以内的摘要。
7. 只返回 JSON，不要其他内容：
{{"summary": "...", "common": "...", "versions": [{{"version": "3.0", "content": "...", "diff_notice": ""}}], "unversioned": "..."}}
其中 versions 必须且只能包含下面 versions 列表给出的标签；unversioned 为版本未标明内容（可为空字符串）。
"""

WIKI_SYNTHESIS_VERSIONED_VERSIONS_HINT = """versions 标签列表：{labels}"""


# ---------------------------------------------------------------------------
# 标题规范化与相似去重
# ---------------------------------------------------------------------------

def normalize_wiki_title(title: str) -> str:
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", title or "")
    t = t.strip().lower()
    t = re.sub(r"[\s\-_\.、。，,;:：；（）()\[\]【】'\"!！?？/\\]+", "", t)
    return t


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_wiki_title(a), normalize_wiki_title(b)
    if na == nb:
        return 1.0
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.9
    sa, sb = set(na), set(nb)
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


TITLE_SIMILARITY_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# 权限域
# ---------------------------------------------------------------------------

def _scope_to_acl_json(scope: AccessScope) -> str:
    return access_control.acl_json_for_scope(scope)


def _page_scope(db: Session, page: Page) -> AccessScope | None:
    if page is None or not page.notebook_id:
        return None
    notebook = db.get(Notebook, page.notebook_id)
    if notebook is None:
        return None
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        return None
    return scope


# ---------------------------------------------------------------------------
# 构建输入
# ---------------------------------------------------------------------------

def _representative_chunks(db: Session, page: Page, limit: int = CONTEXT_CHAR_LIMIT) -> list[str]:
    chunks = (
        db.query(PageChunk)
        .filter(PageChunk.page_id == page.id)
        .order_by(PageChunk.chunk_index)
        .all()
    )
    if not chunks:
        return []
    total = len(chunks)
    indices: set[int] = set()
    if total <= 5:
        indices = set(range(total))
    else:
        indices = {0, total // 2, total - 1}
        step = max(1, total // 6)
        for i in range(0, total, step):
            indices.add(i)
            if len(indices) >= 8:
                break

    picked: list[str] = []
    used = 0
    for i in sorted(indices):
        text = (chunks[i].content or "").strip()
        if not text:
            continue
        if used + len(text) > limit:
            remain = limit - used
            if remain > 40:
                picked.append(text[:remain])
            break
        picked.append(text)
        used += len(text)
    return picked


def _page_text(db: Session, page: Page) -> str:
    title = (page.title or "").strip()
    chunks = _representative_chunks(db, page)
    if chunks:
        body = "\n\n".join(chunks)
        return f"{title}\n{body}" if title else body
    content = (page.content or "").strip()
    if len(content) > CONTEXT_CHAR_LIMIT:
        content = content[:CONTEXT_CHAR_LIMIT] + "\n...(内容过长已截断)"
    return f"{title}\n{content}" if title else content


def _fair_sources_text(db: Session, pages: list[Page], *, total_budget: int = CONTEXT_CHAR_LIMIT * 2) -> str:
    """公平构建多来源上下文：按来源数分配额度，保证后续来源不被前两个挤掉。

    - 每个来源至少保留标题 + 一段代表性内容；
    - 按 per_source_budget 截断；
    - 最后做总长度保护。
    """
    n = max(1, len(pages))
    per_source_budget = max(200, total_budget // n)
    parts: list[str] = []
    for p in pages:
        text = _page_text(db, p)
        title = p.title or "无标题"
        if len(text) > per_source_budget:
            text = text[:per_source_budget] + "\n...(截断)"
        parts.append(f"【{title}】\n{text}")
    joined = "\n\n".join(parts)
    if len(joined) > total_budget:
        joined = joined[:total_budget]
    return joined


def _page_input_hash(title: str, content: str, notebook_id: str, scope_acl_json: str) -> str:
    raw = json.dumps({
        "title": title or "",
        "content": content or "",
        "notebook_id": notebook_id or "",
        "acl_scope": scope_acl_json or "",
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_wiki_synthesis(db: Session, wiki: WikiPage) -> WikiSynthesisSnapshot:
    """生成 Wiki 合成的不可变输入快照。"""
    ordered_ids = tuple(sorted(_parse_source_pages(wiki.source_page_ids)))
    source_hashes: list[str] = []
    for pid in ordered_ids:
        p = db.get(Page, pid)
        if p is None:
            source_hashes.append("")
            continue
        scope = _page_scope(db, p)
        scope_acl = _scope_to_acl_json(scope) if scope else None
        source_hashes.append(_page_input_hash(p.title or "", _page_text(db, p), p.notebook_id or "", scope_acl or ""))

    # locked sections 内容 hash
    locked_payload: list[dict] = []
    if wiki.current_revision_id:
        locked = db.query(WikiSection).filter(
            WikiSection.revision_id == wiki.current_revision_id,
            WikiSection.locked.is_(True),
        ).all()
        locked_payload = [
            {"section_type": s.section_type, "content": s.content or ""}
            for s in locked
        ]
    locked_hash = hashlib.sha256(
        json.dumps(locked_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # 版本来源映射快照（V4 Phase I：版本识别变化也属于 stale_input）。
    version_map_payload = [
        {"page_id": pid, "version": _page_version(db, db.get(Page, pid))["version_label"]}
        for pid in ordered_ids
    ]
    version_map_hash = hashlib.sha256(
        json.dumps(version_map_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    return WikiSynthesisSnapshot(
        wiki_page_id=wiki.id,
        acl_scope=wiki.acl_scope,
        source_page_ids=ordered_ids,
        source_input_hashes=tuple(source_hashes),
        current_revision_id=wiki.current_revision_id,
        locked_sections_hash=locked_hash,
        version_map_hash=version_map_hash,
        title=wiki.title,
        category=wiki.category,
        workspace_id=wiki.workspace_id,
    )


@dataclass(frozen=True)
class PageWikiSnapshot:
    """不可变输入快照，供 LLM 调用期间不持有事务。"""
    page_id: str
    title: str
    content_text: str
    notebook_id: str | None
    scope_acl_json: str | None
    input_hash: str
    workspace_id: str | None = None


@dataclass(frozen=True)
class WikiSynthesisSnapshot:
    """Wiki 合成的不可变输入快照，用于检测 LLM 调用期间的变化。

    LLM 返回后重新计算并与本快照比较，只有完全一致才允许写入 Revision。
    """
    wiki_page_id: str
    acl_scope: str | None
    source_page_ids: tuple[str, ...]
    source_input_hashes: tuple[str, ...]
    current_revision_id: str | None
    locked_sections_hash: str
    title: str
    category: str | None
    version_map_hash: str = ""
    workspace_id: str | None = None


# OpenAI 兼容 chat completions 的输出长度字段。供应商拒绝该参数 → HTTP 4xx →
# raise_for_status 上抛 LLMServiceUnavailable（明确失败，不静默移除限制后继续发送）。
_MAX_OUTPUT_TOKENS_FIELD = "max_tokens"


async def call_wiki_llm_json(messages: list, context: str = "",
                             timeout: float = 120.0,
                             max_output_tokens: int | None = None) -> dict:
    """Wiki 专用的严格 LLM 调用，区分服务不可用与非法响应。

    - 未配置 / 401 / 403 / 500 / 网络失败 / 超时 → 抛 LLMServiceUnavailable。
    - 请求成功但空响应 / 非 JSON → 返回 {}（由调用方判 invalid_response）。
    - max_output_tokens 仅在显式给定时发送（OpenAI 兼容字段 max_tokens），
      未给定 → 载荷与既有行为完全一致。

    不改变 call_llm_json 的默认行为（其他调用方继续返回 {}）。
    """
    if not settings.llm_api_url:
        raise LLMServiceUnavailable("llm not configured")

    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": False,
    }
    if max_output_tokens is not None:
        payload[_MAX_OUTPUT_TOKENS_FIELD] = int(max_output_tokens)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                settings.llm_api_url,
                headers={
                    "Authorization": f"Bearer {settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("wiki llm http error [%s]: %s", context, exc.response.status_code)
        raise LLMServiceUnavailable(f"http {exc.response.status_code}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("wiki llm request failed [%s]: %s", context, exc)
        raise LLMServiceUnavailable(str(exc)) from exc

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content or not content.strip():
        logger.warning("wiki llm empty response [%s]", context)
        return {}

    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', content)
    if json_match:
        content = json_match.group(1)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    brace_depth = 0
    start = -1
    for i, ch in enumerate(content):
        if ch == '{':
            if brace_depth == 0:
                start = i
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
            if brace_depth == 0 and start >= 0:
                candidate = content[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
    logger.warning("wiki llm response not JSON [%s]: %s", context, content[:300])
    return {}


def _snapshot_page(db: Session, page: Page) -> PageWikiSnapshot | None:
    scope = _page_scope(db, page)
    if scope is None:
        return None
    scope_acl = _scope_to_acl_json(scope)
    content_text = _page_text(db, page)
    return PageWikiSnapshot(
        page_id=page.id,
        title=page.title or "无标题",
        content_text=content_text,
        notebook_id=page.notebook_id,
        scope_acl_json=scope_acl,
        workspace_id=_page_workspace_id_with_ensure(db, page),
        # input_hash 必须基于 content_text（含代表性 PageChunk），而非 Page.content，
        # 这样 PageChunk 变化也能被检测。
        input_hash=_page_input_hash(page.title or "", content_text, page.notebook_id or "", scope_acl),
    )


def _page_workspace_id_with_ensure(db: Session, page: Page) -> str | None:
    """解析 Page 的 workspace_id（确定性路由：无 binding 时自动创建，UNKNOWN → None）。"""
    if page is None or not page.notebook_id:
        return None
    notebook = db.get(Notebook, page.notebook_id)
    if notebook is None:
        return None
    ws = ensure_notebook_workspace(db, notebook)
    return ws.id if ws is not None else None


# ---------------------------------------------------------------------------
# 目录与匹配
# ---------------------------------------------------------------------------

def _index_text(wikis: dict[str, dict]) -> str:
    if not wikis:
        return "(暂无页面)"
    lines = []
    for key, p in wikis.items():
        summary = (p.get("summary") or "").replace("\n", " ")[:_INDEX_SUMMARY_LEN]
        lines.append(f"- {p.get('title', '')} | {p.get('category', '') or DEFAULT_CATEGORY} | {summary}")
    return "\n".join(lines)


def _load_scope_wikis(db: Session, scope: AccessScope, workspace_id: str) -> dict[str, dict]:
    """按 (workspace_id + acl_scope) 双条件加载目录（Phase 3.1：workspace_id 必填 str）。

    workspace_id 缺省/None → ValueError（内部编程错误）：禁止 None 时按 ACL 全量回退，
    避免跨 workspace 误 merge / None==None 漏洞。目录查询只属于单个 workspace。
    """
    if not workspace_id:
        raise ValueError("_load_scope_wikis: workspace_id 必填（Topic Router 强制 workspace）")
    acl_json = _scope_to_acl_json(scope)
    rows = (
        db.query(WikiPage)
        .filter(
            WikiPage.acl_scope == acl_json,
            WikiPage.workspace_id == workspace_id,
        )
        .all()
    )
    result: dict[str, dict] = {}
    for wp in rows:
        key = normalize_wiki_title(wp.title)
        result[key] = {
            "id": wp.id,
            "title": wp.title,
            "category": wp.category or DEFAULT_CATEGORY,
            "summary": wp.summary or "",
        }
    return result


def _find_similar_wiki(norm_title: str, wikis: dict[str, dict]) -> dict | None:
    if norm_title in wikis:
        return wikis[norm_title]
    best, best_score = None, 0.0
    for key, p in wikis.items():
        score = title_similarity(norm_title, key)
        if score > best_score:
            best, best_score = p, score
    if best is not None and best_score >= TITLE_SIMILARITY_THRESHOLD:
        return best
    return None


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _parse_source_pages(raw: str | None) -> list[str]:
    try:
        ids = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [i for i in ids if isinstance(i, str)]


def _set_source_pages(page: WikiPage, ids: list[str]) -> None:
    page.source_page_ids = json.dumps(sorted(set(ids)), ensure_ascii=False)


def _add_source_page(page: WikiPage, page_id: str) -> None:
    ids = _parse_source_pages(page.source_page_ids)
    if page_id not in ids:
        ids.append(page_id)
        _set_source_pages(page, ids)


def _build_sections(db: Session, rev: WikiRevision, summary: str, content: str) -> None:
    db.add(WikiSection(
        id=str(uuid.uuid4()), revision_id=rev.id,
        section_type="summary", heading="摘要", content=summary or "", order_index=0, locked=False,
        is_common=True, version_label=versioning.IS_COMMON_LABEL, content_origin="auto",
        merge_policy="auto", version_status="confirmed", version_confidence=1.0,
        version_sort_key=_dump_sort_key(versioning.version_sort_key(versioning.IS_COMMON_LABEL)),
    ))
    db.add(WikiSection(
        id=str(uuid.uuid4()), revision_id=rev.id,
        section_type="facts", heading="正文", content=content or "", order_index=1, locked=False,
        version_label=versioning.UNVERSIONED_LABEL, content_origin="auto",
        merge_policy="auto", version_status="unversioned", version_confidence=0.0,
        version_sort_key=_dump_sort_key(versioning.version_sort_key(versioning.UNVERSIONED_LABEL)),
    ))


# ---------------------------------------------------------------------------
# V4 Phase I：版本分组与来源映射
# ---------------------------------------------------------------------------

def _dump_sort_key(key: tuple) -> str:
    """把 version_sort_key 元组序列化为 JSON 字符串（含非 JSON 类型的 stable tiebreak）。"""
    return json.dumps(list(key), ensure_ascii=False, default=str)


def _page_version(db: Session, page: Page) -> dict:
    """检测 Page 的产品版本（确定性，不依赖 LLM）。

    external_version 不是产品版本，绝不参与。返回检测结果 dict。
    """
    det = versioning.detect_product_version(
        structured_version=None,  # Page 无显式结构化版本字段
        title=page.title,
        source_path=page.source_path,
        content=_page_text(db, page),
    )
    return {
        "version_label": det.version_label,
        "version_status": det.version_status,
        "version_sort_key": _dump_sort_key(det.version_sort_key),
        "candidates": list(det.candidates),
    }


def _group_pages_by_version(db: Session, pages: list[Page]) -> dict[str, list[Page]]:
    """把来源 Page 按产品版本分组（common 由多个版本共同内容决定，不在此分组）。"""
    groups: dict[str, list[Page]] = {}
    for p in pages:
        info = _page_version(db, p)
        label = info["version_label"]
        groups.setdefault(label, []).append(p)
    return groups


def _has_any_version_evidence(db: Session, pages: list[Page]) -> bool:
    """是否存在任何明确版本证据（用于决定是否走版本化合成）。"""
    for p in pages:
        info = _page_version(db, p)
        if info["version_status"] == versioning.VERSION_STATUS_CONFIRMED:
            return True
    return False


@dataclass(frozen=True)
class VersionedContent:
    """版本化合成结果（summary/common/versions/unversioned）。"""
    summary: str = ""
    common: str = ""
    versions: dict[str, str] = field(default_factory=dict)         # label -> content
    diff_notices: dict[str, str] = field(default_factory=dict)      # label -> diff_notice
    unversioned: str = ""
    requested_labels: tuple[str, ...] = ()                          # 本次期望产出的版本标签


def _create_wiki(
    db: Session,
    scope: AccessScope,
    title: str,
    category: str,
    content: str,
    summary: str,
    page_id: str,
    workspace_id: str,
) -> WikiPage:
    """两阶段创建：主题识别阶段只建 draft 占位，不创建 published 空 Revision。

    合成成功后才由 rebuild_wiki_from_sources 创建完整 Revision 并置 published。
    Phase 3：自动编译产生的 Wiki 必须携带 workspace_id —— 必传位置参数，写入端
    强制非空（迁移期列 nullable 不代表可以写入空归属）。
    """
    if not workspace_id:
        raise ValueError("_create_wiki: workspace_id 必须非空（自动编译产生的 Wiki 写入端强制）")
    acl_json = _scope_to_acl_json(scope)
    page = WikiPage(
        id=str(uuid.uuid4()),
        title=title,
        summary=summary,
        category=category or DEFAULT_CATEGORY,
        acl_scope=acl_json,
        status="draft",  # 尚未合成，普通用户不可见
        source_page_ids=json.dumps([page_id], ensure_ascii=False),
        dirty=True,      # 待合成
        locked=False,
        workspace_id=workspace_id,
    )
    db.add(page)
    db.flush()
    return page


def _append_revision(
    db: Session,
    page: WikiPage,
    content: str,
    summary: str,
    *,
    edit_type: str,
    updated_by: str | None = None,
) -> WikiRevision:
    cur_rev = db.query(WikiRevision).filter(WikiRevision.id == page.current_revision_id).first()
    new_rev = WikiRevision(
        id=str(uuid.uuid4()),
        wiki_page_id=page.id,
        parent_revision_id=cur_rev.id if cur_rev else None,
        title=page.title,
        summary=summary,
        source_hash=_content_hash(content),
        status="published",
        edit_type=edit_type,
        updated_by=updated_by,
    )
    db.add(new_rev)
    db.flush()

    seen_types: set[str] = set()
    if cur_rev is not None:
        old_sections = db.query(WikiSection).filter(WikiSection.revision_id == cur_rev.id).order_by(WikiSection.order_index).all()
        for sec in old_sections:
            stype = sec.section_type
            locked = bool(sec.locked)
            if locked:
                new_content = sec.content
            elif stype == "summary":
                new_content = summary
            elif stype in ("facts", "body"):
                new_content = content
            else:
                new_content = sec.content  # 其他 Section 原样保留
            db.add(WikiSection(
                id=str(uuid.uuid4()),
                revision_id=new_rev.id,
                section_type=stype,
                heading=sec.heading,
                content=new_content,
                order_index=sec.order_index,
                locked=locked,
                version_label=sec.version_label,
                version_sort_key=sec.version_sort_key,
                is_common=sec.is_common,
                content_origin=sec.content_origin,
                merge_policy=sec.merge_policy,
                version_confidence=sec.version_confidence,
                version_status=sec.version_status,
                diff_notice=sec.diff_notice,
            ))
            seen_types.add(stype)

    if "summary" not in seen_types:
        db.add(WikiSection(
            id=str(uuid.uuid4()), revision_id=new_rev.id,
            section_type="summary", heading="摘要", content=summary or "", order_index=0, locked=False,
            is_common=True, version_label=versioning.IS_COMMON_LABEL, content_origin="auto",
            merge_policy="auto", version_status="confirmed", version_confidence=1.0,
            version_sort_key=_dump_sort_key(versioning.version_sort_key(versioning.IS_COMMON_LABEL)),
        ))
    if "facts" not in seen_types and "body" not in seen_types:
        db.add(WikiSection(
            id=str(uuid.uuid4()), revision_id=new_rev.id,
            section_type="facts", heading="正文", content=content or "", order_index=1, locked=False,
            version_label=versioning.UNVERSIONED_LABEL, content_origin="auto",
            merge_policy="auto", version_status="unversioned", version_confidence=0.0,
            version_sort_key=_dump_sort_key(versioning.version_sort_key(versioning.UNVERSIONED_LABEL)),
        ))

    page.current_revision_id = new_rev.id
    page.summary = summary
    return new_rev


def _append_versioned_revision(
    db: Session,
    page: WikiPage,
    vc: "VersionedContent",
    *,
    edit_type: str,
    updated_by: str | None = None,
) -> WikiRevision:
    """写版本化 Revision：summary(common) + 每个版本块 + unversioned 块。

    - 版本块 section_type 统一用 "facts"，靠 version_label 区分版本；
      每个版本块一条 WikiSection，人工保护粒度 = 对应版本块。
    - 保留旧 Revision 中 protected 块的对应版本内容（不覆盖）。
    - 汇总 diff_notice 到对应版本块。
    """
    cur_rev = db.query(WikiRevision).filter(WikiRevision.id == page.current_revision_id).first()
    body = vc.common or vc.unversioned or "\n".join(vc.versions.values())
    new_rev = WikiRevision(
        id=str(uuid.uuid4()),
        wiki_page_id=page.id,
        parent_revision_id=cur_rev.id if cur_rev else None,
        title=page.title,
        summary=vc.summary,
        source_hash=_content_hash(body),
        status="published",
        edit_type=edit_type,
        updated_by=updated_by,
    )
    db.add(new_rev)
    db.flush()

    # 旧版本中 protected（人工）的块内容快照：label -> (content, diff_notice)。
    # 版本块与 common 块都按 label 记录，人工保护粒度 = 对应块。
    protected_prev: dict[str, tuple[str, str | None]] = {}
    if cur_rev is not None:
        for sec in db.query(WikiSection).filter(WikiSection.revision_id == cur_rev.id).all():
            label = sec.version_label or (versioning.IS_COMMON_LABEL if sec.is_common else None)
            if sec.merge_policy == "protected" and label:
                protected_prev[label] = (sec.content or "", sec.diff_notice)

    def _add_section(*, label: str, content: str, order: int, is_common: bool, status: str,
                     origin: str, merge_policy: str, confidence: float, diff_notice: str = ""):
        db.add(WikiSection(
            id=str(uuid.uuid4()), revision_id=new_rev.id,
            section_type="facts", heading=None, content=content or "", order_index=order,
            locked=(merge_policy == "protected"),
            version_label=label,
            version_sort_key=_dump_sort_key(versioning.version_sort_key(label)),
            is_common=is_common,
            content_origin=origin,
            merge_policy=merge_policy,
            version_confidence=confidence,
            version_status=status,
            diff_notice=diff_notice,
        ))

    order = 0
    # 1. 通用说明（人工保护时保留旧 common 内容不覆盖）
    if versioning.IS_COMMON_LABEL in protected_prev:
        common_content, common_notice = protected_prev[versioning.IS_COMMON_LABEL]
        _add_section(label=versioning.IS_COMMON_LABEL, content=common_content, order=order,
                     is_common=True, status="confirmed", origin="manual",
                     merge_policy="protected", confidence=1.0, diff_notice=common_notice or "")
    else:
        _add_section(label=versioning.IS_COMMON_LABEL, content=vc.common, order=order,
                     is_common=True, status="confirmed", origin="auto",
                     merge_policy="auto", confidence=1.0)
    order += 1

    # 2. 各版本块（按确定性排序：可比版本 major 降序，最新在前）
    labels = [v for v in vc.versions.keys() if v not in (versioning.IS_COMMON_LABEL, versioning.UNVERSIONED_LABEL)]
    labels.sort(key=versioning.version_sort_key, reverse=True)
    for label in labels:
        content = vc.versions[label]
        diff_notice = vc.diff_notices.get(label, "")
        # 人工保护：该版本块在旧 Revision 中是 protected，则不覆盖。
        if label in protected_prev:
            content, prev_notice = protected_prev[label]
            diff_notice = diff_notice or (prev_notice or "")
            _add_section(label=label, content=content, order=order, is_common=False,
                         status="confirmed", origin="manual", merge_policy="protected",
                         confidence=1.0, diff_notice=diff_notice)
        else:
            _add_section(label=label, content=content, order=order, is_common=False,
                         status="confirmed", origin="auto", merge_policy="auto",
                         confidence=1.0, diff_notice=diff_notice)
        order += 1

    # 3. unversioned 块（版本未标明），仅在有内容时写入
    if (vc.unversioned or "").strip():
        _add_section(label=versioning.UNVERSIONED_LABEL, content=vc.unversioned, order=order,
                     is_common=False, status="unversioned", origin="auto",
                     merge_policy="auto", confidence=0.0)
        order += 1

    page.current_revision_id = new_rev.id
    page.summary = vc.summary
    return new_rev


def _identify_topics(
    db: Session,
    scope: AccessScope,
    ops: list[dict],
    wikis: dict[str, dict],
    page_id: str,
    workspace_id: str,
) -> tuple[int, int, set[str]]:
    """识别主题并建立/更新来源关系，不写正文（正文由聚合函数负责）。

    返回 (created, updated, target_wiki_ids)。
    """
    if not ops:
        return 0, 0, set()
    created = updated = 0
    target_ids: set[str] = set()
    for op in ops[:MAX_TOPICS_PER_PAGE]:
        action = op.get("action")
        title = (op.get("title") or "").strip()
        if not title or action not in ("create", "update"):
            continue
        category = (op.get("category") or "").strip()[:128]
        norm = normalize_wiki_title(title)
        existing = _find_similar_wiki(norm, wikis)
        if existing is not None:
            page = db.get(WikiPage, existing["id"])
            if page is not None:
                page.category = category or page.category or DEFAULT_CATEGORY
                page.dirty = True  # 新增来源，需重新聚合
                _add_source_page(page, page_id)
                target_ids.add(page.id)
                updated += 1
        else:
            page = _create_wiki(db, scope, title, category, "", "", page_id, workspace_id)
            wikis[norm] = {
                "id": page.id, "title": page.title,
                "category": page.category or DEFAULT_CATEGORY, "summary": "",
            }
            target_ids.add(page.id)
            created += 1
    return created, updated, target_ids


async def _legacy_rebuild_wiki_from_sources(
    db: Session,
    wiki_page_id: str,
    llm: Callable,
    *,
    commit: bool = True,
) -> dict:
    """主题级聚合：读该 Wiki 全部有效 source Page，按产品版本合成一个聚合 Revision。

    V4 Phase I 版本感知：
    - 来源按产品版本确定性分组（versioning），不得把 external_version 当产品版本。
    - 通用说明（common）只放多版本共同内容；版本块分别合成。
    - 人工保护粒度 = 对应版本块：旧 Revision 中 merge_policy=protected 的版本块不覆盖。
    - 快照含版本与版本来源映射，LLM 期间变化 → stale_input 丢弃结果。
    - LLM 服务不可用 / 非法响应保持 dirty，不覆盖当前 Revision。
    """
    wiki = db.get(WikiPage, wiki_page_id)
    if wiki is None:
        return {"wiki_id": wiki_page_id, "status": "deleted"}

    if wiki.workspace_id is None:
        # Phase 3.1：无 workspace 归属的 dirty wiki → archived（fail closed）。
        # 禁止发布到 NULL workspace，避免 None==None 与来源 page 漏校验时的 stale_input 无限循环。
        wiki.status = "archived"
        wiki.dirty = False
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "archived"}

    source_ids = _parse_source_pages(wiki.source_page_ids)
    if not source_ids:
        wiki.status = "archived"
        wiki.dirty = False
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "archived"}

    snapshot_before = _snapshot_wiki_synthesis(db, wiki)
    ordered_ids = list(snapshot_before.source_page_ids)
    pages = [db.get(Page, pid) for pid in ordered_ids]
    pages = [p for p in pages if p is not None]

    if not pages:
        wiki.status = "archived"
        wiki.dirty = False
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "archived"}

    # 结束读取事务，LLM 调用期间不持有事务
    db.commit()

    # V4 Phase I：是否有版本证据决定走版本化还是传统单正文路径。
    # 无版本证据 → 传统路径（unversioned），保持历史 Section 结构与兼容。
    versioned = _has_any_version_evidence(db, pages)

    try:
        if versioned:
            vc = await _synthesize_versioned(db, wiki, pages, llm)
            content = summary = None
        else:
            vc = None
            content, summary = await _synthesize_content(db, wiki, pages, "", llm)
    except LLMServiceUnavailable as exc:
        logger.warning("wiki synthesis unavailable for %s: %s", wiki_page_id, exc)
        wiki = db.get(WikiPage, wiki_page_id)
        if wiki is not None:
            wiki.dirty = True
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "service_unavailable"}

    if versioned and vc is None:
        wiki = db.get(WikiPage, wiki_page_id)
        if wiki is not None:
            wiki.dirty = True
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "invalid_response"}
    if not versioned and content is None:
        wiki = db.get(WikiPage, wiki_page_id)
        if wiki is not None:
            wiki.dirty = True
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "invalid_response"}

    # 重新查询并重新计算快照，只有完全一致才写入
    wiki = db.get(WikiPage, wiki_page_id)
    if wiki is None:
        return {"wiki_id": wiki_page_id, "status": "deleted"}

    snapshot_after = _snapshot_wiki_synthesis(db, wiki)
    if snapshot_after != snapshot_before:
        logger.warning("wiki synthesis stale input for %s", wiki_page_id)
        wiki.dirty = True
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "stale_input"}

    # 校验每个来源 Page 的 ACL 与 WikiPage.acl_scope 一致 + workspace 一致（fail closed）
    for pid in snapshot_after.source_page_ids:
        p = db.get(Page, pid)
        if p is None:
            continue
        scope = _page_scope(db, p)
        if scope is None or _scope_to_acl_json(scope) != wiki.acl_scope:
            wiki.dirty = True
            if commit:
                db.commit()
            logger.warning("wiki synthesis acl mismatch for %s", wiki_page_id)
            return {"wiki_id": wiki_page_id, "status": "stale_input"}
        if page_workspace_id(db, p) != wiki.workspace_id:
            # Phase 3：来源 Page 的 workspace 归属不一致 → fail closed，保持 dirty。
            wiki.dirty = True
            if commit:
                db.commit()
            logger.warning("wiki synthesis workspace mismatch for %s", wiki_page_id)
            return {"wiki_id": wiki_page_id, "status": "stale_input"}

    if versioned:
        _append_versioned_revision(db, wiki, vc, edit_type="auto")
        _sync_version_sources(db, wiki, pages)
        rows = db.query(WikiSection.version_label).filter(
            WikiSection.revision_id == wiki.current_revision_id
        ).all()
        labels = [
            r[0] for r in rows
            if r[0] and r[0] not in (versioning.IS_COMMON_LABEL, versioning.UNVERSIONED_LABEL)
        ]
        wiki.latest_version = versioning.latest_version(labels)
    else:
        _append_revision(db, wiki, content, summary, edit_type="auto")
        _sync_version_sources(db, wiki, pages)
    wiki.status = "published"
    wiki.dirty = False
    if commit:
        db.commit()
    return {"wiki_id": wiki_page_id, "status": "success"}


def _sync_version_sources(db: Session, wiki: WikiPage, pages: list[Page]) -> None:
    """同步 wiki_version_sources 关联表（后台 version → Page 映射，前端不展示）。

    - 按 page_id 重建来源映射：每个 Page 按其检测到的版本标签写入一条。
    - 真实映射粒度 = version → Page；chunk_id / source_item_id 本次不填充。
    - 不跨 scope：来源 Page 的实际 acl_scope 与 wiki.acl_scope 不一致时不写。
    - 不跨 workspace：来源 Page 的实际 workspace 与 wiki.workspace_id 不一致时不写（Phase 3）。
    """
    scope = wiki.acl_scope
    db.query(WikiVersionSource).filter(WikiVersionSource.wiki_page_id == wiki.id).delete()
    for p in pages:
        p_scope = _page_scope(db, p)
        if p_scope is None or _scope_to_acl_json(p_scope) != scope:
            continue  # 防止跨 scope 合并
        if page_workspace_id(db, p) != wiki.workspace_id:
            continue  # 防止跨 workspace 合并
        info = _page_version(db, p)
        label = info["version_label"]
        db.add(WikiVersionSource(
            id=str(uuid.uuid4()),
            wiki_page_id=wiki.id,
            version_label=label,
            page_id=p.id,
            chunk_id=None,
            source_item_id=None,
            acl_scope=scope,
        ))


def _load_existing_versioned(db: Session, wiki: WikiPage) -> tuple[str, dict[str, str]]:
    """读取当前 Revision 的通用说明与各版本正文（供合成提示词保留）。

    返回 (existing_common, {label: content})。
    """
    existing_common = ""
    existing_versions: dict[str, str] = {}
    cur_rev = db.query(WikiRevision).filter(WikiRevision.id == wiki.current_revision_id).first()
    if cur_rev is not None:
        for sec in db.query(WikiSection).filter(WikiSection.revision_id == cur_rev.id).all():
            if sec.is_common or sec.version_label == versioning.IS_COMMON_LABEL:
                existing_common = sec.content or ""
            elif sec.version_label and sec.version_label != versioning.UNVERSIONED_LABEL:
                existing_versions[sec.version_label] = sec.content or ""
    return existing_common, existing_versions


async def _synthesize_versioned(
    db: Session,
    wiki: WikiPage,
    pages: list[Page],
    llm: Callable,
) -> "VersionedContent | None":
    """版本化合成：确定性分组 + LLM 生成 common/versions/unversioned。

    返回 VersionedContent；服务不可用抛 LLMServiceUnavailable；非法响应返回 None。
    无任何版本证据时回退到非版本化单正文合成（unversioned），保持旧行为兼容。
    """
    existing_common, existing_versions = _load_existing_versioned(db, wiki)

    # 无版本证据 → 回退旧单正文合成（unversioned）。
    if not _has_any_version_evidence(db, pages):
        content, summary = await _synthesize_content(db, wiki, pages, existing_common, llm)
        if content is None:
            return None
        return VersionedContent(summary=summary, common="", versions={}, unversioned=content)

    # 分组：相同版本合并来源；同版本去重由 LLM + 确定性分组共同保证。
    groups = _group_pages_by_version(db, pages)
    # 可比较版本标签列表（确定性排序），供提示词明确允许的版本。
    version_labels = [label for label in groups.keys()
                      if label not in (versioning.IS_COMMON_LABEL, versioning.UNVERSIONED_LABEL, "ambiguous")]
    version_labels.sort(key=versioning.version_sort_key)

    sources_text = _versioned_sources_text(db, groups)
    existing_versions_text = _existing_versions_text(existing_versions)
    prompt = WIKI_SYNTHESIS_VERSIONED_PROMPT.format(
        title=wiki.title,
        category=wiki.category or DEFAULT_CATEGORY,
        existing_common=existing_common[:CONTEXT_CHAR_LIMIT],
        existing_versions=existing_versions_text,
        sources=sources_text,
        versions=WIKI_SYNTHESIS_VERSIONED_VERSIONS_HINT.format(labels=", ".join(version_labels)),
    )
    result = await _call_synthesis_llm(llm, prompt, "wiki-synthesis")
    if not isinstance(result, dict):
        return None

    summary = (result.get("summary") or "").strip()[:200]
    common = (result.get("common") or "").strip()
    unversioned = (result.get("unversioned") or "").strip()
    raw_versions = result.get("versions")
    versions: dict[str, str] = {}
    diff_notices: dict[str, str] = {}
    if isinstance(raw_versions, list):
        for item in raw_versions:
            if not isinstance(item, dict):
                continue
            label = versioning.normalize_version_label(item.get("version") or "")
            # 只接受本次明确允许的版本标签（LLM 不得新增版本）。
            if label not in version_labels:
                continue
            content = (item.get("content") or "").strip()
            if content:
                versions[label] = content
            notice = (item.get("diff_notice") or "").strip()
            if notice:
                diff_notices[label] = notice

    # 若 LLM 漏掉某版本，回退用该版本来源文本兜底（保证不丢版本内容）。
    for label in version_labels:
        if label not in versions:
            versions[label] = _version_group_text(db, groups.get(label, [])) or ""

    if not summary and not common and not versions and not unversioned:
        return None

    return VersionedContent(
        summary=summary, common=common, versions=versions,
        diff_notices=diff_notices, unversioned=unversioned,
        requested_labels=tuple(version_labels),
    )


def _versioned_sources_text(db: Session, groups: dict[str, list[Page]]) -> str:
    """按版本分组渲染来源文本（确定性顺序）。"""
    parts: list[str] = []
    for label in sorted(groups.keys(), key=versioning.version_sort_key):
        pages = groups[label]
        body = _fair_sources_text(db, pages)
        parts.append(f"== 版本 {label} ==\n{body}")
    return "\n\n".join(parts)


def _version_group_text(db: Session, pages: list[Page]) -> str:
    return _fair_sources_text(db, pages) if pages else ""


def _existing_versions_text(existing_versions: dict[str, str]) -> str:
    if not existing_versions:
        return "(暂无)"
    parts = []
    for label in sorted(existing_versions.keys(), key=versioning.version_sort_key):
        parts.append(f"== 版本 {label} ==\n{existing_versions[label]}")
    return "\n\n".join(parts)


async def _synthesize_content(
    db: Session,
    wiki: WikiPage,
    pages: list[Page],
    existing_body: str,
    llm: Callable,
) -> tuple[str | None, str]:
    """合成正文：来源少时单次合成；来源超预算时 Map-Reduce。

    返回 (content, summary)；content=None 表示 invalid_response。
    服务不可用抛 LLMServiceUnavailable。
    """
    # 判断是否需要 Map-Reduce：来源总原始长度超过单次预算，或来源数量过多。
    # 不能用 _fair_sources_text 的截断结果判断，否则永远触发不了 Map-Reduce。
    raw_total = sum(len(_page_text(db, p)) for p in pages)
    if raw_total <= CONTEXT_CHAR_LIMIT * 2 and len(pages) <= 6:
        sources_text = _fair_sources_text(db, pages)
        prompt = WIKI_SYNTHESIS_PROMPT.format(
            title=wiki.title,
            category=wiki.category or DEFAULT_CATEGORY,
            existing=existing_body[:CONTEXT_CHAR_LIMIT],
            sources=sources_text,
        )
        result = await _call_synthesis_llm(llm, prompt, "wiki-synthesis")
        if not isinstance(result, dict):
            return None, ""
        content = (result.get("content") or "").strip()
        summary = (result.get("summary") or "").strip()[:200]
        if not content:
            return None, summary
        return content, summary

    # Map-Reduce：分批中间摘要 → 最终合成
    return await _mapreduce_synthesize(db, wiki, pages, existing_body, llm)


async def _call_synthesis_llm(llm: Callable, prompt: str, context: str) -> dict:
    """调用合成 LLM，区分服务不可用（抛异常）与非法响应（返回非 dict）。"""
    try:
        return await llm([{"role": "user", "content": prompt}], context=context)
    except LLMServiceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("synthesis llm error [%s]: %s", context, exc)
        raise LLMServiceUnavailable(str(exc)) from exc


async def _mapreduce_synthesize(
    db: Session,
    wiki: WikiPage,
    pages: list[Page],
    existing_body: str,
    llm: Callable,
) -> tuple[str | None, str]:
    """Map-Reduce：分层 Reduce，避免最终硬截断丢弃后面的批次。

    - 第一层：把大量来源分组生成中间摘要；
    - 后续层：若摘要总量仍超预算，把摘要再分组生成更上层摘要，逐层归并；
    - 每一层所有摘要都进入下一层某个批次，顺序稳定；
    - 设置最大层数防止异常循环；任一层失败不覆盖当前 Revision。
    """
    # 第一层：来源 → 摘要
    batch_size = 4
    summaries: list[str] = []
    for start in range(0, len(pages), batch_size):
        batch = pages[start:start + batch_size]
        batch_text = _fair_sources_text(db, batch)
        prompt = WIKI_BATCH_SUMMARY_PROMPT.format(title=wiki.title, sources=batch_text)
        result = await _call_synthesis_llm(llm, prompt, "wiki-batch-summary")
        if not isinstance(result, dict):
            return None, ""
        summary = (result.get("summary") or "").strip()
        if not summary:
            return None, ""
        summaries.append(summary)

    # 分层归并：摘要总长超预算时继续向上合并
    max_layers = 8
    layer = 0
    while layer < max_layers:
        joined = "\n\n".join(summaries)
        if len(joined) <= CONTEXT_CHAR_LIMIT * 2:
            break
        # 把当前摘要列表分组再生成更上层摘要
        next_summaries: list[str] = []
        for start in range(0, len(summaries), batch_size):
            chunk = summaries[start:start + batch_size]
            prompt = WIKI_BATCH_SUMMARY_PROMPT.format(title=wiki.title, sources="\n\n".join(chunk))
            result = await _call_synthesis_llm(llm, prompt, "wiki-batch-summary")
            if not isinstance(result, dict):
                return None, ""
            summary = (result.get("summary") or "").strip()
            if not summary:
                return None, ""
            next_summaries.append(summary)
        summaries = next_summaries
        layer += 1

    # 分层归并后重新计算 joined；仍超预算则不再硬截断，直接放弃（reduce_limit_exceeded）。
    joined = "\n\n".join(summaries)
    if len(joined) > CONTEXT_CHAR_LIMIT * 2:
        logger.warning("wiki mapreduce reduce_limit_exceeded for %s", wiki.id)
        return None, ""

    final_prompt = WIKI_MAPREDUCE_PROMPT.format(
        title=wiki.title,
        category=wiki.category or DEFAULT_CATEGORY,
        existing=existing_body[:CONTEXT_CHAR_LIMIT],
        summaries=joined,
    )
    result = await _call_synthesis_llm(llm, final_prompt, "wiki-mapreduce")
    if not isinstance(result, dict):
        return None, ""
    content = (result.get("content") or "").strip()
    summary = (result.get("summary") or "").strip()[:200]
    if not content:
        return None, summary
    return content, summary


# ---------------------------------------------------------------------------
# 来源关系协调
# ---------------------------------------------------------------------------

def reconcile_page_wiki_membership(
    db: Session,
    page_id: str,
    target_wiki_ids: set[str],
) -> dict:
    """集中协调 Page 与 Wiki 的来源关系，返回结构化结果。

    返回：
    - retained_target_ids：仍是该 Page 目标（保留来源）的 Wiki id
    - dirty_remaining_wiki_ids：解除该 Page 后仍有其他来源、需要重算的 Wiki id
    - archived_wiki_ids：解除后无来源、已归档的 Wiki id

    规则：
    - target_wiki_ids 是 LLM 成功返回并应用后的该 Page 新目标 Wiki 集合。
    - 找出所有 source_page_ids 含 page_id 的 Wiki，对不在 target 的解除来源。
    - 解除后仍有其他来源 → dirty=True。
    - 解除后无来源 → archived + dirty=False。
    """
    retained_target_ids: set[str] = set()
    dirty_remaining_wiki_ids: set[str] = set()
    archived_wiki_ids: set[str] = set()

    rows = db.query(WikiPage).filter(WikiPage.source_page_ids.isnot(None)).all()
    for wp in rows:
        ids = _parse_source_pages(wp.source_page_ids)
        if page_id not in ids:
            continue
        if wp.id in target_wiki_ids:
            retained_target_ids.add(wp.id)
            continue
        ids = [i for i in ids if i != page_id]
        # V4 Phase I：同步移除该 Page 的版本来源映射（保持后台来源映射一致）。
        db.query(WikiVersionSource).filter(
            WikiVersionSource.wiki_page_id == wp.id,
            WikiVersionSource.page_id == page_id,
        ).delete()
        if ids:
            _set_source_pages(wp, ids)
            wp.dirty = True
            # 安全：多来源移除后，立即从正式检索/普通用户展示中排除旧 Revision，
            # 直到用剩余来源重建成功再重新 published。
            wp.status = "draft"
            dirty_remaining_wiki_ids.add(wp.id)
        else:
            wp.source_page_ids = "[]"
            wp.status = "archived"
            wp.dirty = False
            archived_wiki_ids.add(wp.id)

    return {
        "retained_target_ids": retained_target_ids,
        "dirty_remaining_wiki_ids": dirty_remaining_wiki_ids,
        "archived_wiki_ids": archived_wiki_ids,
    }


def remove_source_page_from_wikis(db: Session, page_id: str, *, commit: bool = True) -> dict:
    """删除 Page 后移除来源：唯一来源 → archived；多来源 → 移除 + dirty。

    返回 {"dirty_remaining_wiki_ids": set, "archived_wiki_ids": set}。
    """
    rows = db.query(WikiPage).filter(WikiPage.source_page_ids.isnot(None)).all()
    dirty_remaining_wiki_ids: set[str] = set()
    archived_wiki_ids: set[str] = set()
    for wp in rows:
        ids = _parse_source_pages(wp.source_page_ids)
        if page_id in ids:
            ids = [i for i in ids if i != page_id]
            # V4 Phase I：同步移除该 Page 的版本来源映射。
            db.query(WikiVersionSource).filter(
                WikiVersionSource.wiki_page_id == wp.id,
                WikiVersionSource.page_id == page_id,
            ).delete()
            if ids:
                _set_source_pages(wp, ids)
                wp.dirty = True
                wp.status = "draft"  # 安全：重建成功前排除正式检索
                dirty_remaining_wiki_ids.add(wp.id)
            else:
                wp.source_page_ids = "[]"
                wp.status = "archived"
                wp.dirty = False
                archived_wiki_ids.add(wp.id)
    if (dirty_remaining_wiki_ids or archived_wiki_ids) and commit:
        db.commit()
    elif (dirty_remaining_wiki_ids or archived_wiki_ids):
        db.flush()
    return {
        "dirty_remaining_wiki_ids": dirty_remaining_wiki_ids,
        "archived_wiki_ids": archived_wiki_ids,
    }


# ---------------------------------------------------------------------------
# LLM 状态判定
# ---------------------------------------------------------------------------

@dataclass
class LlmOutcome:
    status: str  # success / not_worthy / service_unavailable / invalid_response
    ops: list[dict]


def _llm_outcome(result) -> LlmOutcome:
    """把 LLM 返回结果归一化为明确状态。

    严格协议（V4 Phase C 收口）：
    - worthy is False + ops=[] → not_worthy（唯一允许解除旧来源的情况）
    - worthy is True + 至少一个合法 op → success
    - worthy is True + ops=[] → invalid_response
    - worthy 缺失 → invalid_response
    - ops 缺失/非列表 → invalid_response
    - ops 全部非法项 → invalid_response

    合法 op：action ∈ create/update；title 非空字符串；category 类型合法（str/None）。
    """
    if result is None:
        return LlmOutcome("service_unavailable", [])
    if not isinstance(result, dict):
        return LlmOutcome("invalid_response", [])

    worthy = result.get("worthy")
    ops = result.get("ops")

    # 显式无价值 → not_worthy（唯一会解除旧来源的分支）
    if worthy is False:
        return LlmOutcome("not_worthy", [])

    # 有价值：必须有 ops 列表，且至少一个合法 op
    if worthy is True:
        if not isinstance(ops, list):
            return LlmOutcome("invalid_response", [])
        valid = [op for op in ops if _valid_op(op)]
        if not valid:
            return LlmOutcome("invalid_response", [])
        return LlmOutcome("success", valid)

    # 缺 worthy 字段 → 协议不符，视为非法响应（不得当 not_worthy）
    return LlmOutcome("invalid_response", [])


def _valid_op(op) -> bool:
    """判定单个 op 是否合法。"""
    if not isinstance(op, dict):
        return False
    action = op.get("action")
    if action not in ("create", "update"):
        return False
    title = op.get("title")
    if not isinstance(title, str) or not title.strip():
        return False
    category = op.get("category")
    if category is not None and not isinstance(category, str):
        return False
    return True


# ---------------------------------------------------------------------------
# 单 Page 处理（快照 → 结束事务 → LLM → 重查对比 → 应用）
# ---------------------------------------------------------------------------

async def _legacy_process_page_wiki(
    db: Session,
    page_id: str,
    llm: Callable,
    *,
    commit: bool = True,
    synthesize: bool = True,
) -> dict:
    """处理单个 Page 的 Wiki 构建/刷新。

    synthesize=False 时只做主题识别与来源协调（不合成正文），返回
    target_wiki_ids / dirty_remaining_wiki_ids 供上层统一聚合（全量重建去重用）。
    返回该 Page 的 outcome 状态 dict。事务边界由本函数控制。
    """
    page = db.get(Page, page_id)
    if page is None:
        return {"page_id": page_id, "status": "deleted"}
    snapshot = _snapshot_page(db, page)
    if snapshot is None:
        # 有 Notebook 但 scope 无法解析（unknown）→ 同样是「未绑定 fail closed」，
        # 保持 dirty + no_workspace_binding；无 Notebook 归属 → 旧语义 no_scope。
        if page.notebook_id and db.get(Notebook, page.notebook_id) is not None:
            fresh = db.get(Page, page_id)
            if fresh is not None:
                fresh.wiki_dirty = True
                fresh.wiki_last_error = "no_workspace_binding"
            if commit:
                db.commit()
            return {"page_id": page_id, "status": "no_workspace"}
        return {"page_id": page_id, "status": "no_scope"}
    if snapshot.workspace_id is None:
        # Phase 3 fail closed：未绑定 Page 进入 Topic Router → no_workspace，
        # 不回退同 ACL 全局 Wiki。
        fresh = db.get(Page, page_id)
        if fresh is not None:
            fresh.wiki_dirty = True
            fresh.wiki_last_error = "no_workspace_binding"
        if commit:
            db.commit()
        return {"page_id": page_id, "status": "no_workspace"}
    if not snapshot.content_text or len(snapshot.content_text.strip()) < MIN_CONTENT_CHARS:
        # 内容过短：视为 not_worthy，解除来源
        membership = _finalize_page(db, page, snapshot, "not_worthy", set(), commit=commit)
        return {
            "page_id": page_id,
            "status": "not_worthy",
            "dirty_remaining_wiki_ids": membership["dirty_remaining_wiki_ids"],
        }

    # 在同一读取事务内取齐快照与 Wiki 目录索引，再统一结束事务
    index = _index_text(_load_scope_wikis_from_snapshot(db, snapshot.scope_acl_json, snapshot.workspace_id))
    db.commit()  # 结束读取事务，LLM 调用期间不持有事务

    # 调用 LLM（不持有事务）
    try:
        result = await llm(
            [{"role": "user", "content": WIKI_INGEST_PROMPT.format(
                index=index, title=snapshot.title, content=snapshot.content_text[:CONTEXT_CHAR_LIMIT],
            )}],
            context="wiki-ingest-page",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("wiki llm unavailable for page %s: %s", page_id, exc)
        _mark_page_error(db, page_id, "service_unavailable")
        return {"page_id": page_id, "status": "service_unavailable"}

    outcome = _llm_outcome(result)

    # 重新查询 Page，对比输入是否变化（用相同 _page_text 算法重算 content_text）
    fresh = db.get(Page, page_id)
    if fresh is None:
        return {"page_id": page_id, "status": "deleted"}

    fresh_scope = _page_scope(db, fresh)
    fresh_scope_acl = _scope_to_acl_json(fresh_scope) if fresh_scope else None
    fresh_content_text = _page_text(db, fresh)
    fresh_hash = _page_input_hash(fresh.title or "", fresh_content_text, fresh.notebook_id or "", fresh_scope_acl or "")
    fresh_workspace_id = _page_workspace_id_with_ensure(db, fresh)

    # Phase 3：workspace 无法解析 → fail closed（保持 dirty + no_workspace_binding）
    if fresh_workspace_id is None:
        fresh.wiki_dirty = True
        fresh.wiki_last_error = "no_workspace_binding"
        if commit:
            db.commit()
        return {"page_id": page_id, "status": "no_workspace"}

    # notebook_id / ACL scope / workspace 变化 → 解除旧域来源，保持 dirty 等待新域重建
    if (
        fresh.notebook_id != snapshot.notebook_id
        or fresh_scope_acl != snapshot.scope_acl_json
        or fresh_workspace_id != snapshot.workspace_id
    ):
        reconcile_page_wiki_membership(db, page_id, set())
        fresh.wiki_dirty = True
        fresh.wiki_last_error = "scope_changed_during_llm"
        if commit:
            db.commit()
        return {"page_id": page_id, "status": "scope_changed"}

    # 内容/标题/PageChunk 变化 → 丢弃旧结果，保持 dirty 下次重算
    if fresh_hash != snapshot.input_hash:
        fresh.wiki_dirty = True
        fresh.wiki_last_error = "input_changed_during_llm"
        if commit:
            db.commit()
        return {"page_id": page_id, "status": "stale_input"}

    # 输入未变化，应用结果
    if outcome.status == "success":
        scope = fresh_scope
        wikis = _load_scope_wikis(db, scope, snapshot.workspace_id)
        created, updated, target_ids = _identify_topics(
            db, scope, outcome.ops, wikis, page_id, snapshot.workspace_id
        )
        membership = reconcile_page_wiki_membership(db, page_id, target_ids)
        if commit:
            db.commit()  # 主题识别与来源协调先行提交

        to_synthesize = set(target_ids) | membership["dirty_remaining_wiki_ids"]

        # 只识别不合成：供全量重建统一聚合（每个 Wiki 只合成一次）
        if not synthesize:
            fresh = db.get(Page, page_id)
            if fresh is not None:
                fresh.wiki_dirty = True  # 留待合成成功后清除
                fresh.wiki_last_error = None
                if commit:
                    db.commit()
            return {
                "page_id": page_id,
                "status": "identified",
                "created": created,
                "updated": updated,
                "target_wiki_ids": target_ids,
                "dirty_remaining_wiki_ids": membership["dirty_remaining_wiki_ids"],
                "to_synthesize": to_synthesize,
                "identification_input_hash": snapshot.input_hash,
            }

        synthesis_results: dict[str, str] = {}
        for wiki_id in to_synthesize:
            try:
                r = await _legacy_rebuild_wiki_from_sources(db, wiki_id, llm, commit=commit)
                synthesis_results[wiki_id] = r.get("status", "failed")
            except Exception as exc:  # noqa: BLE001
                logger.exception("wiki synthesis failed for %s: %s", wiki_id, exc)
                db.rollback()
                synthesis_results[wiki_id] = "failed"

        # 只有全部合成成功 + 当前输入未变化才清 Page 状态。
        all_ok = all(s == "success" for s in synthesis_results.values())
        fresh = db.get(Page, page_id)
        if fresh is not None:
            if not all_ok:
                fresh.wiki_dirty = True
                fresh.wiki_last_error = "wiki_synthesis_failed"
            else:
                # 并发窗口保护：重新计算当前 input_hash，与识别时快照一致才清 dirty。
                cur_scope = _page_scope(db, fresh)
                cur_scope_acl = _scope_to_acl_json(cur_scope) if cur_scope else None
                cur_hash = _page_input_hash(
                    fresh.title or "", _page_text(db, fresh), fresh.notebook_id or "", cur_scope_acl or ""
                )
                if cur_hash == snapshot.input_hash:
                    fresh.wiki_dirty = False
                    fresh.wiki_compiled_content_hash = snapshot.input_hash
                    fresh.wiki_last_error = None
                else:
                    fresh.wiki_dirty = True
                    fresh.wiki_last_error = "input_changed_during_synthesis"
                    all_ok = False
            if commit:
                db.commit()

        if all_ok:
            return {"page_id": page_id, "status": "success", "created": created, "updated": updated}
        return {"page_id": page_id, "status": "partial", "created": created, "updated": updated}

    if outcome.status == "not_worthy":
        membership = _finalize_page(db, fresh, snapshot, "not_worthy", set(), commit=commit)
        # 有剩余来源的旧 Wiki 需要重合成（已是 dirty + draft）
        return {
            "page_id": page_id,
            "status": "not_worthy",
            "dirty_remaining_wiki_ids": membership["dirty_remaining_wiki_ids"],
        }

    # service_unavailable / invalid_response
    fresh.wiki_dirty = True
    fresh.wiki_last_error = outcome.status
    if commit:
        db.commit()
    return {"page_id": page_id, "status": outcome.status}


def _load_scope_wikis_from_snapshot(
    db: Session,
    scope_acl_json: str | None,
    workspace_id: str,
) -> dict[str, dict]:
    """从 acl_scope JSON 字符串加载目录（快照阶段用，避免解析 AccessScope）。

    Phase 3.1：双条件（workspace_id + acl_scope）强制 —— workspace_id 必填非空；
    None → ValueError（内部编程错误），禁止 None 时按 ACL 全量回退。
    """
    if not workspace_id:
        raise ValueError("_load_scope_wikis_from_snapshot: workspace_id 必填（Topic Router 强制 workspace）")
    if not scope_acl_json:
        return {}
    rows = (
        db.query(WikiPage)
        .filter(
            WikiPage.acl_scope == scope_acl_json,
            WikiPage.workspace_id == workspace_id,
        )
        .all()
    )
    result: dict[str, dict] = {}
    for wp in rows:
        result[normalize_wiki_title(wp.title)] = {
            "id": wp.id, "title": wp.title,
            "category": wp.category or DEFAULT_CATEGORY, "summary": wp.summary or "",
        }
    return result


def _finalize_page(db: Session, page: Page, snapshot: PageWikiSnapshot, status: str, target_ids: set[str], *, commit: bool) -> dict:
    """完成 not_worthy / 内容过短：解除来源并清 dirty（不视为服务故障）。

    返回 membership 结果，调用方必须处理 dirty_remaining_wiki_ids（有剩余来源的
    Wiki 已在 reconcile 中置 dirty + draft，需要调度重合成）。
    """
    membership = reconcile_page_wiki_membership(db, page.id, target_ids)
    page.wiki_dirty = False
    page.wiki_compiled_content_hash = snapshot.input_hash
    page.wiki_last_error = None
    if commit:
        db.commit()
    else:
        db.flush()
    return membership


def _mark_page_error(db: Session, page_id: str, error: str) -> None:
    """LLM 不可用时标记 Page 待重试，不覆盖现有 Wiki。"""
    page = db.get(Page, page_id)
    if page is not None:
        page.wiki_dirty = True
        page.wiki_last_error = error
    db.commit()


# ---------------------------------------------------------------------------
# 构建 / 刷新入口
# ---------------------------------------------------------------------------

async def _legacy_build_wiki_from_pages(
    db: Session,
    pages: list[Page],
    *,
    llm_json: Callable = None,
    commit: bool = True,
    dedupe_synthesis: bool = True,
) -> dict:
    """Page 驱动的 Wiki 构建入口。

    dedupe_synthesis=True（全量重建默认）时采用两阶段：
    1. 识别全部 Page 的主题并协调来源，收集唯一 affected wiki ids；
    2. 每个 Wiki 只聚合一次；
    3. 根据聚合结果更新相关 Page 状态。
    """
    llm = llm_json or call_wiki_llm_json
    stats = {"created": 0, "updated": 0, "skipped": 0, "failed": 0,
             "service_unavailable": 0, "not_worthy": 0, "invalid_response": 0}

    if not dedupe_synthesis or len(pages) <= 1:
        return await _legacy_build_pages_incremental(db, pages, llm, commit, stats)

    # ---- 两阶段：先识别，后去重聚合 ----
    page_to_synthesize: dict[str, set[str]] = {}
    page_identification_hash: dict[str, str] = {}
    affected_wiki_ids: set[str] = set()
    for page in pages:
        try:
            outcome = await _legacy_process_page_wiki(db, page.id, llm, commit=commit, synthesize=False)
            s = outcome.get("status")
            if s == "identified":
                stats["created"] += outcome.get("created", 0)
                stats["updated"] += outcome.get("updated", 0)
                to_syn = outcome.get("to_synthesize") or set()
                page_to_synthesize[page.id] = set(to_syn)
                page_identification_hash[page.id] = outcome.get("identification_input_hash") or ""
                affected_wiki_ids.update(to_syn)
            elif s == "not_worthy":
                stats["not_worthy"] += 1
                # not_worthy 解除来源后，有剩余来源的旧 Wiki 必须重新合成
                dirty = outcome.get("dirty_remaining_wiki_ids") or set()
                affected_wiki_ids.update(dirty)
            elif s == "service_unavailable":
                stats["service_unavailable"] += 1
                stats["skipped"] += 1
            elif s == "invalid_response":
                stats["invalid_response"] += 1
                stats["failed"] += 1
            elif s == "no_scope":
                stats["failed"] += 1
            elif s == "no_workspace":
                stats["failed"] += 1
            elif s in ("deleted", "scope_changed", "stale_input"):
                stats["skipped"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            stats["failed"] += 1
            logger.exception("wiki identify failed for page %s: %s", page.id, exc)

    # 唯一 wiki 只聚合一次
    wiki_results: dict[str, str] = {}
    for wiki_id in sorted(affected_wiki_ids):
        try:
            r = await _legacy_rebuild_wiki_from_sources(db, wiki_id, llm, commit=commit)
            wiki_results[wiki_id] = r.get("status", "failed")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            wiki_results[wiki_id] = "failed"
            logger.exception("wiki synthesis failed for %s: %s", wiki_id, exc)

    # 根据聚合结果更新 Page 状态（含识别时 input_hash 并发窗口保护）
    for page_id, wiki_ids in page_to_synthesize.items():
        if not wiki_ids:
            continue
        all_ok = all(wiki_results.get(wid) == "success" for wid in wiki_ids)
        page = db.get(Page, page_id)
        if page is None:
            continue
        if not all_ok:
            page.wiki_dirty = True
            page.wiki_last_error = "wiki_synthesis_failed"
            continue
        # 重新计算当前 input_hash，与识别时一致才清 dirty
        cur_scope = _page_scope(db, page)
        cur_scope_acl = _scope_to_acl_json(cur_scope) if cur_scope else None
        cur_hash = _page_input_hash(
            page.title or "", _page_text(db, page), page.notebook_id or "", cur_scope_acl or ""
        )
        ident_hash = page_identification_hash.get(page_id, "")
        if cur_hash == ident_hash:
            page.wiki_dirty = False
            page.wiki_compiled_content_hash = ident_hash
            page.wiki_last_error = None
        else:
            page.wiki_dirty = True
            page.wiki_last_error = "input_changed_during_synthesis"
    if commit:
        db.commit()
    return stats


async def _legacy_build_pages_incremental(db, pages, llm, commit, stats) -> dict:
    """单 Page / 增量路径：逐 Page 识别+合成。"""
    for page in pages:
        try:
            outcome = await _legacy_process_page_wiki(db, page.id, llm, commit=commit, synthesize=True)
            s = outcome.get("status")
            if s == "success":
                stats["created"] += outcome.get("created", 0)
                stats["updated"] += outcome.get("updated", 0)
            elif s == "not_worthy":
                stats["not_worthy"] += 1
            elif s == "service_unavailable":
                stats["service_unavailable"] += 1
                stats["skipped"] += 1
            elif s == "invalid_response":
                stats["invalid_response"] += 1
                stats["failed"] += 1
            elif s == "partial":
                stats["failed"] += 1
            elif s == "no_scope":
                stats["failed"] += 1
            elif s == "no_workspace":
                stats["failed"] += 1
            elif s in ("deleted", "scope_changed", "stale_input"):
                stats["skipped"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            stats["failed"] += 1
            logger.exception("wiki build failed for page %s: %s", page.id, exc)
    return stats


async def _legacy_refresh_wiki_for_page(
    db: Session,
    page: Page,
    *,
    llm_json: Callable = None,
    commit: bool = True,
) -> dict:
    """单 Page 增量刷新（数据源同步 / Page 更新后调用）。"""
    llm = llm_json or call_wiki_llm_json
    return await _legacy_build_wiki_from_pages(db, [page], llm_json=llm, commit=commit)


# ---------------------------------------------------------------------------
# dirty 刷新（Wiki 级，每 Wiki 独立事务 + scope 校验）
# ---------------------------------------------------------------------------

async def _legacy_refresh_dirty_wikis(
    db: Session,
    *,
    llm_json: Callable = None,
    commit: bool = True,
) -> dict:
    """刷新所有 dirty=True 的 Page 驱动 Wiki（主题级聚合）。

    每个 Wiki 独立处理；任一 Wiki 失败不回滚其他 Wiki。
    校验 source_page_ids 中每个 Page 的实际 ACL scope 与 WikiPage.acl_scope 一致，
    不一致 fail closed（不得在旧权限域继续发布）。

    只有以下终态才清 dirty：
    - 聚合 Revision 成功提交（rebuild_wiki_from_sources 成功）；
    - 无来源并成功 archived；
    - scope 不一致并成功 archived。
    """
    llm = llm_json or call_wiki_llm_json
    dirty_wikis = db.query(WikiPage).filter(WikiPage.dirty.is_(True)).all()
    refreshed = failed = archived = 0
    for wp in dirty_wikis:
        try:
            # Phase 3.1：无 workspace 归属的 dirty wiki → archived（fail closed）。
            # 禁止向 NULL workspace 发布，避免来源 page 也解析不出 workspace 时 None==None 通过。
            if wp.workspace_id is None:
                wp.status = "archived"
                wp.dirty = False
                archived += 1
                if commit:
                    db.commit()
                logger.warning("wiki refresh missing workspace_id, archived wiki %s", wp.id)
                continue

            page_ids = _parse_source_pages(wp.source_page_ids)
            if not page_ids:
                wp.status = "archived"
                wp.dirty = False
                archived += 1
                if commit:
                    db.commit()
                continue

            pages = db.query(Page).filter(Page.id.in_(page_ids)).all()
            if not pages:
                wp.status = "archived"
                wp.dirty = False
                archived += 1
                if commit:
                    db.commit()
                continue

            # scope 校验：每个 source page 的实际 scope 必须与 Wiki 一致；
            # Phase 3：每个 source page 的 workspace 必须与 Wiki 一致（缺失/不一致 → archived）。
            scope_ok = True
            for p in pages:
                scope = _page_scope(db, p)
                if scope is None or _scope_to_acl_json(scope) != wp.acl_scope:
                    scope_ok = False
                    break
                if page_workspace_id(db, p) != wp.workspace_id:
                    scope_ok = False
                    break
            if not scope_ok:
                wp.status = "archived"
                wp.dirty = False
                archived += 1
                if commit:
                    db.commit()
                logger.warning("wiki refresh scope/workspace mismatch, archived wiki %s", wp.id)
                continue

            # 主题级聚合：读全部 source 合成正文
            outcome = await _legacy_rebuild_wiki_from_sources(db, wp.id, llm, commit=commit)
            if outcome.get("status") == "success":
                refreshed += 1
            else:
                # 非终态（service_unavailable / invalid_response）保持 dirty
                failed += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            failed += 1
            logger.exception("refresh dirty wiki failed for %s: %s", wp.id, exc)
    return {"refreshed": refreshed, "failed": failed, "archived": archived}


# ---------------------------------------------------------------------------
# 公开 CompileRun wrapper（Phase 5.2.1）
#
# 旧公开入口 `build_wiki_from_pages` / `process_page_wiki` /
# `rebuild_wiki_from_sources` / `refresh_wiki_for_page` / `refresh_dirty_wikis`
# 不再指向 `_legacy_*`（等价实现仅供旧测试**显式调用** `_legacy_*` 名）。
# 同名公开函数现在是真正的 wiki.default CompileRun wrapper：
#   - build_wiki_from_pages     → 按 Workspace 建 batch_rebuild CompileRun；
#   - refresh_wiki_for_page     → page_changed CompileRun；
#   - rebuild_wiki_from_sources → manual_rebuild CompileRun；
#   - refresh_dirty_wikis       → 逐 dirty Wiki manual_rebuild CompileRun；
#   - process_page_wiki         → 无安全公开兼容语义，已私有化（_legacy_process_page_wiki）。
# wrapper 保持原 async 签名与返回结构，内部把 Publish Manifest 转换为兼容结果。
# 生产代码不得通过这些公开名称进入 _legacy_*；wrapper 一律执行真实 pipeline run。
# ---------------------------------------------------------------------------

_ZERO_COMPILE_STATS = {
    "created": 0, "updated": 0, "skipped": 0, "failed": 0,
    "service_unavailable": 0, "not_worthy": 0, "invalid_response": 0,
}


def _compile_page_input_hash(db, page_id: str) -> str:
    """Page 当前内容/范围确定性 input_hash（与 scheduler 口径一致，供 run 幂等）。"""
    page = db.get(Page, page_id)
    if page is None:
        return ""
    scope = _page_scope(db, page)
    acl = _scope_to_acl_json(scope) if scope else None
    return _page_input_hash(
        page.title or "", _page_text(db, page), page.notebook_id or "", acl or ""
    )


def _compile_wiki_input_hash(db, wiki) -> str:
    """Wiki 重建确定性 input_hash（标题 + 归属 + 来源 Page，供 manual_rebuild 幂等）。"""
    parts = [wiki.title or "", wiki.acl_scope or "", wiki.workspace_id or ""]
    for pid in sorted(_parse_source_pages(wiki.source_page_ids)):
        parts.append(pid)
        page = db.get(Page, pid)
        if page is None:
            parts.append("")
            continue
        scope = _page_scope(db, page)
        acl = _scope_to_acl_json(scope) if scope else None
        parts.append(_page_input_hash(
            page.title or "", _page_text(db, page), page.notebook_id or "", acl or ""
        ))
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_publish_manifest(db, run_id: str) -> dict | None:
    """读本 run 最新 wiki_publish_manifest Artifact payload（wrapper 结果归一用）。"""
    from app.models.database import KnowledgeCompileArtifact as _Artifact

    row = (
        db.query(_Artifact)
        .filter(
            _Artifact.run_id == run_id,
            _Artifact.artifact_type == "wiki_publish_manifest",
        )
        .order_by(_Artifact.created_at.desc())
        .first()
    )
    if row is None or not row.payload_json:
        return None
    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _compile_run_and_execute(db, *, trigger_type, trigger_object_id, workspace_id,
                             wiki_page_id=None, input_hash="", idempotency_key=None):
    """建 wiki.default queued run 并同步执行（公共 wrapper 内部）。

    要求 wiki.default pipeline 已注册；LLM/graph runner 按 executor 契约配置（缺省走
    真实 LLM / 真实图谱）。idempotency_key 命中既有 run：queued → 执行；已终态 →
    直接返回既有 run（不重复执行/发布）。与 scheduler 同口径（supersede 旧非终态）。
    """
    from app.core.wiki_pipeline import executor as _exec
    from app.core.wiki_pipeline.pipelines.wiki_default import PIPELINE_KEY as _PK

    run = _exec.create_run(
        db,
        pipeline_key=_PK,
        trigger_type=trigger_type,
        trigger_object_id=trigger_object_id,
        workspace_id=workspace_id,
        wiki_page_id=wiki_page_id,
        input_hash=input_hash or None,
        idempotency_key=idempotency_key,
        supersede_same_trigger=True,
    )
    db.commit()
    if run.status in ("queued", "running", "failed"):
        executed = _exec.execute_run(db, run.id)
        db.expire_all()
        return executed
    db.expire_all()
    return run


def _maybe_configure_llm_runner(llm_json) -> None:
    """llm_json（async）未注入 runner 时包装为同步 runner 兜底（同 multi_page）。"""
    from app.core.wiki_pipeline import executor as _exec

    if llm_json is None or _exec._LLM_RUNNER is not None:  # noqa: SLF001
        return

    def _sync_wrap(messages, context: str = "", timeout: float = 120.0) -> dict:
        return asyncio.run(llm_json(messages, context=context, timeout=timeout))

    _exec.configure_external_runners(llm_runner=_sync_wrap)


def _build_wiki_from_pages_sync(
    db: Session,
    pages: list[Page],
    *,
    llm_json: Callable = None,
    commit: bool = True,
    dedupe_synthesis: bool = True,
) -> dict:
    """（同步核心，线程内执行）公开 wrapper 的 batch_rebuild CompileRun 逻辑。

    返回结构与旧版本兼容：{created, updated, skipped, failed, service_unavailable,
    not_worthy, invalid_response}（由各 workspace batch run 的 Publish Manifest 归一）。
    无 workspace 归属的 Page 不进入 batch（保持 dirty、计入 failed），绝不回退 legacy。
    """
    from app.core.wiki_pipeline.pipelines.multi_page import batch_build

    if not pages:
        return dict(_ZERO_COMPILE_STATS)
    groups: dict[str, list[str]] = {}
    unbound: list[str] = []
    for p in pages:
        ws = page_workspace_id(db, p) or _page_workspace_id_with_ensure(db, p)
        if ws:
            groups.setdefault(ws, []).append(p.id)
        else:
            unbound.append(p.id)
    stats = dict(_ZERO_COMPILE_STATS)
    for ws_id in sorted(groups):
        part = batch_build(db, ws_id, groups[ws_id], llm_json=llm_json, commit=commit)
        for k in stats:
            stats[k] += int(part.get(k) or 0)
    if unbound:
        stats["failed"] += len(unbound)
        if commit:
            for pid in unbound:
                fresh = db.get(Page, pid)
                if fresh is not None:
                    fresh.wiki_dirty = True
                    fresh.wiki_last_error = "no_workspace_binding"
            db.commit()
    return stats


async def build_wiki_from_pages(
    db: Session,
    pages: list[Page],
    *,
    llm_json: Callable = None,
    commit: bool = True,
    dedupe_synthesis: bool = True,
) -> dict:
    """公开 CompileRun wrapper：按 Workspace 建 batch_rebuild run 并同步执行。

    保持旧 async 签名与返回结构。pipeline stage 内部使用 asyncio.run 执行真实 LLM/
    图谱（需非事件循环线程），故通过 asyncio.to_thread 在独立工作线程执行同步核心。
    """
    return await asyncio.to_thread(
        _build_wiki_from_pages_sync, db, list(pages),
        llm_json=llm_json, commit=commit, dedupe_synthesis=dedupe_synthesis,
    )


def _single_page_changed_stats(db, run, pre_wiki_ids: set[str]) -> dict:
    """page_changed run 结果 → 旧单 Page 返回键 stats（created/updated 由 Manifest 判定）。"""
    stats = dict(_ZERO_COMPILE_STATS)
    if run.status != "succeeded":
        code = run.safe_error_code or ""
        if code == "SERVICE_UNAVAILABLE":
            stats["service_unavailable"] = 1
            stats["skipped"] = 1
            return stats
        if code == "INVALID_RESPONSE":
            stats["invalid_response"] = 1
            stats["failed"] = 1
            return stats
        if code == "STALE_INPUT":
            stats["skipped"] = 1
            return stats
        stats["failed"] = 1
        return stats
    manifest = _read_publish_manifest(db, run.id) or {}
    note = manifest.get("note") or ""
    if manifest.get("outcome") == "not_worthy" or note.startswith("not_worthy"):
        stats["not_worthy"] = 1
        return stats
    for wid in manifest.get("wiki_page_ids") or []:
        if wid in pre_wiki_ids:
            stats["updated"] += 1
        else:
            stats["created"] += 1
    return stats


def _refresh_wiki_for_page_sync(
    db: Session,
    page: Page,
    *,
    llm_json: Callable = None,
    commit: bool = True,
) -> dict:
    """（同步核心，线程内执行）单 Page 刷新 → page_changed CompileRun。

    返回结构与旧 refresh_wiki_for_page 兼容（单 Page 构建 stats）。无 workspace 归属 →
    保持 dirty + failed（fail closed），不回退 legacy。
    """
    from app.core.wiki_pipeline.pipelines.wiki_default import make_default_idempotency_key

    if page is None or not getattr(page, "id", None):
        return dict(_ZERO_COMPILE_STATS)
    page_id = page.id
    ws = page_workspace_id(db, page) or _page_workspace_id_with_ensure(db, page)
    scope = _page_scope(db, page)
    acl = _scope_to_acl_json(scope) if scope else None
    if not ws or scope is None or acl is None:
        fresh = db.get(Page, page_id)
        if fresh is not None:
            fresh.wiki_dirty = True
            fresh.wiki_last_error = "no_workspace_binding" if not ws else "no_scope"
            if commit:
                db.commit()
        stats = dict(_ZERO_COMPILE_STATS)
        stats["failed"] = 1
        return stats
    pre_wiki_ids = {
        w[0] for w in db.query(WikiPage.id).filter(
            WikiPage.acl_scope == acl, WikiPage.workspace_id == ws
        ).all()
    }
    input_hash = _compile_page_input_hash(db, page_id)
    key = make_default_idempotency_key(
        workspace_id=ws, trigger_type="page_changed",
        trigger_object_id=page_id, wiki_page_id="", full_input_hash=input_hash,
    )
    _maybe_configure_llm_runner(llm_json)
    run = _compile_run_and_execute(
        db, trigger_type="page_changed", trigger_object_id=page_id,
        workspace_id=ws, input_hash=input_hash, idempotency_key=key,
    )
    return _single_page_changed_stats(db, run, pre_wiki_ids)


async def refresh_wiki_for_page(
    db: Session,
    page: Page,
    *,
    llm_json: Callable = None,
    commit: bool = True,
) -> dict:
    """公开 CompileRun wrapper：单 Page 刷新 → page_changed CompileRun 并同步执行。

    保持旧 async 签名与返回结构；经 asyncio.to_thread 在线程内执行（pipeline stage
    内部 asyncio.run 需非事件循环线程）。
    """
    return await asyncio.to_thread(
        _refresh_wiki_for_page_sync, db, page,
        llm_json=llm_json, commit=commit,
    )


def _manual_rebuild_status(db, run, wiki_page_id: str) -> str:
    """manual_rebuild run → 旧 rebuild_wiki_from_sources 的 status 语义。"""
    if run.status != "succeeded":
        code = run.safe_error_code or ""
        if code == "SERVICE_UNAVAILABLE":
            return "service_unavailable"
        if code == "INVALID_RESPONSE":
            return "invalid_response"
        if code == "STALE_INPUT":
            return "stale_input"
        if db.get(WikiPage, wiki_page_id) is None:
            return "deleted"
        return "failed"
    manifest = _read_publish_manifest(db, run.id) or {}
    outcome = manifest.get("outcome")
    if outcome == "published":
        return "success"
    if outcome == "archived":
        return "archived"
    if db.get(WikiPage, wiki_page_id) is None:
        return "deleted"
    return "success"


def _rebuild_wiki_from_sources_sync(
    db: Session,
    wiki_page_id: str,
    llm: Callable,
    *,
    commit: bool = True,
) -> dict:
    """（同步核心，线程内执行）dirty/目标 Wiki 重合成 → manual_rebuild CompileRun。

    返回结构与旧版兼容：{"wiki_id": ..., "status": success|archived|deleted|...}。
    llm 参数为签名兼容保留（真实编译走 executor 注入的 runner），不进入 _legacy_*。
    """
    from app.core.wiki_pipeline.pipelines.wiki_default import make_default_idempotency_key

    wiki = db.get(WikiPage, wiki_page_id)
    if wiki is None:
        return {"wiki_id": wiki_page_id, "status": "deleted"}
    if not wiki.workspace_id:
        wiki.status = "archived"
        wiki.dirty = False
        if commit:
            db.commit()
        return {"wiki_id": wiki_page_id, "status": "archived"}
    input_hash = _compile_wiki_input_hash(db, wiki)
    key = make_default_idempotency_key(
        workspace_id=wiki.workspace_id, trigger_type="manual_rebuild",
        trigger_object_id="", wiki_page_id=wiki_page_id, full_input_hash=input_hash,
    )
    _maybe_configure_llm_runner(llm)
    run = _compile_run_and_execute(
        db, trigger_type="manual_rebuild", trigger_object_id=wiki_page_id,
        workspace_id=wiki.workspace_id, wiki_page_id=wiki_page_id,
        input_hash=input_hash, idempotency_key=key,
    )
    return {"wiki_id": wiki_page_id, "status": _manual_rebuild_status(db, run, wiki_page_id)}


async def rebuild_wiki_from_sources(
    db: Session,
    wiki_page_id: str,
    llm: Callable,
    *,
    commit: bool = True,
) -> dict:
    """公开 CompileRun wrapper：dirty/目标 Wiki 重合成 → manual_rebuild CompileRun。

    保持旧 async 签名与返回结构；经 asyncio.to_thread 在线程内执行（pipeline stage
    内部 asyncio.run 需非事件循环线程）。
    """
    return await asyncio.to_thread(
        _rebuild_wiki_from_sources_sync, db, wiki_page_id, llm, commit=commit,
    )


def _refresh_dirty_wikis_sync(
    db: Session,
    *,
    llm_json: Callable = None,
    commit: bool = True,
) -> dict:
    """（同步核心，线程内执行）刷新全部 dirty Wiki → 逐 Wiki manual_rebuild CompileRun。

    返回结构与旧版兼容：{"refreshed": n, "failed": n, "archived": n}。无 workspace 归属
    的 dirty wiki 直接 archived（镜像旧 fail-closed），不回退 legacy。
    """
    from app.core.wiki_pipeline.pipelines.wiki_default import make_default_idempotency_key

    dirty_wikis = db.query(WikiPage).filter(WikiPage.dirty.is_(True)).all()
    refreshed = failed = archived = 0
    for wp in dirty_wikis:
        if wp.workspace_id is None:
            wp.status = "archived"
            wp.dirty = False
            archived += 1
            if commit:
                db.commit()
            continue
        input_hash = _compile_wiki_input_hash(db, wp)
        key = make_default_idempotency_key(
            workspace_id=wp.workspace_id, trigger_type="manual_rebuild",
            trigger_object_id="", wiki_page_id=wp.id, full_input_hash=input_hash,
        )
        _maybe_configure_llm_runner(llm_json)
        run = _compile_run_and_execute(
            db, trigger_type="manual_rebuild", trigger_object_id=wp.id,
            workspace_id=wp.workspace_id, wiki_page_id=wp.id,
            input_hash=input_hash, idempotency_key=key,
        )
        status = _manual_rebuild_status(db, run, wp.id)
        if status == "success":
            refreshed += 1
        elif status == "archived":
            archived += 1
        else:
            failed += 1
    return {"refreshed": refreshed, "failed": failed, "archived": archived}


async def refresh_dirty_wikis(
    db: Session,
    *,
    llm_json: Callable = None,
    commit: bool = True,
) -> dict:
    """公开 CompileRun wrapper：刷新全部 dirty Wiki → 逐 Wiki manual_rebuild CompileRun。

    保持旧 async 签名与返回结构；经 asyncio.to_thread 在线程内执行（pipeline stage
    内部 asyncio.run 需非事件循环线程）。
    """
    return await asyncio.to_thread(
        _refresh_dirty_wikis_sync, db, llm_json=llm_json, commit=commit,
    )
