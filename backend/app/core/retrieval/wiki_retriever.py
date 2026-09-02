"""Wiki 优先检索器（V4 Phase D-1 最终验收补丁）。

只检索 status=published 的 Wiki，只读取经过归属校验的 current_revision_id
指向的 Revision 与 Sections 正文。必须先执行 Phase B ACL 过滤。

Revision 归属校验（fail closed）：
- WikiRevision.id == wiki.current_revision_id
- WikiRevision.wiki_page_id == wiki.id
- WikiRevision.status == "published"
任一不满足 → 该 Wiki 视为无效，不读 Sections，不入 hits，不凭 title/summary 判命中。

硬约束：不 import / 读取 KnowledgeCard、KnowledgeCardBlock、KnowledgeCommunity、旧 KO。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core import access_control
from app.core.knowledge_compiler_v3 import versioning
from app.models.database import WikiPage, WikiRevision, WikiSection

try:
    import jieba
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False

# 仅保留中文/英文/数字（jieba 不可用时的回退；fullmatch 用于过滤 token）
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]+")

# 常见问句/停用词（本地确定性停用词表）
_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "有", "我", "你", "他", "她", "它",
    "们", "这", "那", "请", "请问", "如何", "怎么", "怎样", "什么", "哪些", "哪",
    "是否", "相关", "问题", "一个", "一下", "可以", "应该", "需要", "给", "把", "被",
    "就", "都", "也", "又", "还", "很", "更", "最", "吗", "呢", "吧", "啊", "呀",
}


def meaningful_tokenize(text: str) -> list[str]:
    """本地有意义 token 化（不修改全局 BM25 tokenize）。

    - NFKC 归一化；
    - 英文转小写；
    - 仅保留中文/英文/数字 token（去掉纯标点）；
    - 删除常见问句/停用词。
    """
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower()
    if _JIEBA_AVAILABLE:
        tokens = [t.strip() for t in jieba.lcut(text) if t.strip()]
    else:
        tokens = _TOKEN_RE.findall(text)
    # 仅保留中文/英文/数字，去纯标点与停用词
    result = []
    for t in tokens:
        if not _TOKEN_RE.fullmatch(t):
            continue
        if t in _STOPWORDS:
            continue
        result.append(t)
    return result


def _normalize_title(title: str) -> str:
    return "".join(meaningful_tokenize(title or ""))


# V4 Phase I：查询版本三态。
QUERY_VERSION_UNSPECIFIED = "unspecified"  # 无版本 → common + latest
QUERY_VERSION_SPECIFIED = "specified"      # 明确指定一个版本 → common + 该版本
QUERY_VERSION_AMBIGUOUS = "ambiguous"      # 同时出现多个版本 → 不选 latest，不猜


@dataclass(frozen=True)
class QueryVersionState:
    """查询版本三态判定结果。"""

    state: str            # unspecified / specified / ambiguous
    version_label: str | None = None  # specified 时为规范化版本标签


def detect_query_version(question: str) -> QueryVersionState:
    """从查询中提取产品版本，返回明确三态（确定性，不猜版本）。

    - 无版本 → unspecified（走 common + latest）。
    - 明确指定一个版本 → specified（走 common + 该版本）。
    - 同时出现多个互不相同的版本 → ambiguous（绝不选 latest、绝不猜）。
    """
    labels = versioning.extract_version_labels(question or "")
    if not labels:
        return QueryVersionState(state=QUERY_VERSION_UNSPECIFIED)
    unique = list(dict.fromkeys(labels))
    if len(unique) > 1:
        return QueryVersionState(state=QUERY_VERSION_AMBIGUOUS)
    return QueryVersionState(state=QUERY_VERSION_SPECIFIED, version_label=unique[0])


def _section_content_for_version(
    db: Session,
    wiki: WikiPage,
    rev: WikiRevision,
    qv: QueryVersionState,
) -> tuple[str, str | None, str | None]:
    """按版本组装 Wiki 正文。

    三态行为：
    - unspecified 且 latest_version 存在：只 common + latest_version，不混入 unversioned。
    - unspecified 且 latest_version 不存在：common + unversioned，version_label 保持 None。
    - specified：common + 指定版本块；版本不存在则只 common（绝不回退其他版本，
      也绝不回退 unversioned）。
    - ambiguous：只 common（不选 latest、不猜版本）。

    返回 (content, matched_version_label, diff_notice)。
    - matched_version_label 为 None 表示未命中任何具体版本（common-only 或 ambiguous）。
    - 绝不调用 _wiki_content 回退全部正文。
    """
    common_parts: list[str] = []
    unversioned_parts: list[str] = []
    version_parts: list[str] = []
    matched_label: str | None = None
    diff_notice: str | None = None

    # 解析目标版本：仅 specified 或 unspecified 且存在 latest 时才有目标。
    target: str | None = None
    if qv.state == QUERY_VERSION_SPECIFIED:
        target = qv.version_label
    elif qv.state == QUERY_VERSION_UNSPECIFIED:
        target = wiki.latest_version
    # ambiguous → target 保持 None，只返回 common。

    sections = (
        db.query(WikiSection)
        .filter(WikiSection.revision_id == rev.id)
        .order_by(WikiSection.order_index)
        .all()
    )
    for sec in sections:
        label = sec.version_label
        if sec.is_common or label == versioning.IS_COMMON_LABEL:
            if sec.content:
                common_parts.append(sec.content)
            continue
        # 版本未标明（含历史无 version_label 的旧 facts/body 块）。
        if label in (None, "", versioning.UNVERSIONED_LABEL):
            if sec.content:
                unversioned_parts.append(sec.content)
            continue
        # 具体版本块
        if target is not None and label == target:
            if sec.content:
                version_parts.append(sec.content)
            matched_label = target
            if sec.diff_notice:
                diff_notice = sec.diff_notice

    if qv.state == QUERY_VERSION_SPECIFIED:
        parts = common_parts + version_parts
    elif qv.state == QUERY_VERSION_AMBIGUOUS:
        parts = common_parts
    elif target is not None:
        # unspecified 且 latest 存在：只 common + latest，不混入 unversioned。
        parts = common_parts + version_parts
    else:
        # unspecified 且 latest 不存在：common + unversioned，matched_label 保持 None。
        parts = common_parts + unversioned_parts

    content = "\n\n".join(p for p in parts if p and p.strip())
    return content, matched_label, diff_notice


def _wiki_content_versioned(
    db: Session,
    wiki: WikiPage,
    rev: WikiRevision,
    qv: QueryVersionState,
) -> tuple[str, str | None, str | None]:
    """版本感知正文聚合。

    - 指定版本不存在 / ambiguous 且 common 为空 → 返回空串，绝不用
      _wiki_content 回退全部版本正文（否则会把 2.0/3.0 专属内容错误返回给
      指定 9.0 的用户）。
    - 返回 (content, matched_version_label, diff_notice)。
    """
    content, matched_label, diff_notice = _section_content_for_version(db, wiki, rev, qv)
    return content, matched_label, diff_notice


def _dedupe(tokens: list[str]) -> list[str]:
    """保持首次出现顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        if t in seen:
            continue
        seen.add(t)
        result.append(t)
    return result


@dataclass
class WikiHit:
    """单个 Wiki 命中结果。"""
    wiki_page_id: str
    title: str
    summary: str
    content: str
    score: float
    acl_scope: str | None
    # 可解释匹配信息（供充分性判断；query/matched/missing 均已去重）
    query_tokens: list[str] = field(default_factory=list)
    matched_tokens: list[str] = field(default_factory=list)
    missing_tokens: list[str] = field(default_factory=list)
    query_token_count: int = 0
    coverage: float = 0.0
    exact_title_match: bool = False
    title_contained: bool = False
    # V4 Phase I：版本字段（用户可见，不含来源信息）。
    version_label: str | None = None
    is_common: bool = False
    latest_version: str | None = None
    diff_notice: str | None = None


@dataclass
class WikiRetrievalResult:
    """WikiRetriever 返回结构。"""
    hits: list[WikiHit] = field(default_factory=list)
    visible_wiki_count: int = 0
    published_wiki_count: int = 0
    query: str = ""


def _valid_current_revision(db: Session, wiki: WikiPage) -> WikiRevision | None:
    """校验 current_revision_id 的归属与状态（fail closed）。

    必须同时满足：
    - WikiRevision.id == wiki.current_revision_id
    - WikiRevision.wiki_page_id == wiki.id
    - WikiRevision.status == "published"
    任一不满足返回 None。
    """
    if not wiki.current_revision_id:
        return None
    rev = (
        db.query(WikiRevision)
        .filter(
            WikiRevision.id == wiki.current_revision_id,
            WikiRevision.wiki_page_id == wiki.id,
            WikiRevision.status == "published",
        )
        .first()
    )
    return rev


def _wiki_content(db: Session, wiki: WikiPage, rev: WikiRevision) -> str:
    """聚合经过校验的当前 Revision 的正文（summary + 所有 Section，按 order 排序）。"""
    parts: list[str] = []
    if wiki.summary:
        parts.append(wiki.summary)
    if rev.summary:
        parts.append(rev.summary)
    sections = (
        db.query(WikiSection)
        .filter(WikiSection.revision_id == rev.id)
        .order_by(WikiSection.order_index)
        .all()
    )
    for sec in sections:
        if sec.heading:
            parts.append(sec.heading)
        if sec.content:
            parts.append(sec.content)
    return "\n".join(p for p in parts if p and p.strip())


def _compute_match(question: str, wiki: WikiPage, content: str) -> dict:
    """计算可解释匹配信息（query token 去重，匹配范围 = 标题 + 正文）。"""
    q_tokens = _dedupe(meaningful_tokenize(question))
    title_tokens = _dedupe(meaningful_tokenize(wiki.title or ""))
    content_tokens = _dedupe(meaningful_tokenize(content))

    # 允许的标题范围 + 正文 = 可命中范围
    searchable = set(title_tokens) | set(content_tokens)
    matched = [t for t in q_tokens if t in searchable]
    missing = [t for t in q_tokens if t not in searchable]
    coverage = (len(matched) / len(q_tokens)) if q_tokens else 0.0

    norm_q = _normalize_title(question)
    norm_title = _normalize_title(wiki.title or "")
    # 精确标题匹配：norm_q == norm_title；标题仅包含在更长问题中：norm_title in norm_q 且不相等。
    exact_title_match = bool(norm_title) and norm_q == norm_title
    title_contained = bool(norm_title) and norm_title in norm_q and norm_q != norm_title

    return {
        "query_tokens": q_tokens,
        "matched_tokens": matched,
        "missing_tokens": missing,
        "query_token_count": len(q_tokens),
        "coverage": round(coverage, 4),
        "exact_title_match": exact_title_match,
        "title_contained": title_contained,
    }


def retrieve_wiki(
    db: Session,
    current_user: dict,
    question: str,
    *,
    top_k: int = 5,
) -> WikiRetrievalResult:
    """检索当前用户可见、published、且当前 Revision 归属合法的 Wiki。"""
    result = WikiRetrievalResult(query=question)

    # 1. ACL 过滤（必须先执行）
    visible_ids = access_control.get_visible_wiki_page_ids(db, current_user)
    result.visible_wiki_count = len(visible_ids)
    if not visible_ids:
        return result

    # 2. 只取 published + 可见
    published_wikis = (
        db.query(WikiPage)
        .filter(
            WikiPage.id.in_(visible_ids),
            WikiPage.status == "published",
            WikiPage.current_revision_id.isnot(None),
        )
        .all()
    )
    result.published_wiki_count = len(published_wikis)
    if not published_wikis:
        return result

    # V4 Phase I：查询版本三态（unspecified / specified / ambiguous）。
    qv = detect_query_version(question)

    # 3. 逐个校验 Revision 归属，只有合法归属才可参与打分
    scored: list[tuple[float, WikiPage, dict, str | None, str | None]] = []
    for wiki in published_wikis:
        rev = _valid_current_revision(db, wiki)
        if rev is None:
            # fail closed：Revision 归属/状态非法，不入 hits
            continue
        content, matched_version, diff_notice = _wiki_content_versioned(db, wiki, rev, qv)
        match = _compute_match(question, wiki, content)
        matched = match["matched_tokens"]
        if not matched and not match["exact_title_match"]:
            continue
        # 打分：覆盖率 + 命中数（summary 不重复拼接，这里只用 content 与 title）
        score = round(0.7 * match["coverage"] + 0.3 * min(len(matched), 10) / 10.0, 4)
        scored.append((score, wiki, match, matched_version, diff_notice))

    scored.sort(key=lambda x: x[0], reverse=True)

    for score, wiki, match, matched_version, diff_notice in scored[:top_k]:
        rev = _valid_current_revision(db, wiki)
        if rev is not None:
            content, matched_version, diff_notice = _wiki_content_versioned(db, wiki, rev, qv)
        else:
            content = ""
        result.hits.append(WikiHit(
            wiki_page_id=wiki.id,
            title=wiki.title,
            summary=wiki.summary or "",
            content=content,
            score=score,
            acl_scope=wiki.acl_scope,
            query_tokens=match["query_tokens"],
            matched_tokens=match["matched_tokens"],
            missing_tokens=match["missing_tokens"],
            query_token_count=match["query_token_count"],
            coverage=match["coverage"],
            exact_title_match=match["exact_title_match"],
            title_contained=match["title_contained"],
            version_label=matched_version,
            is_common=matched_version is None,
            latest_version=wiki.latest_version,
            diff_notice=diff_notice,
        ))
    return result


class WikiRetriever:
    """WikiRetriever 类封装。"""

    name = "wiki"

    def __init__(self, db: Session):
        self.db = db

    def retrieve(
        self,
        db: Session,
        question: str,
        current_user: dict,
        *,
        top_k: int = 5,
    ) -> WikiRetrievalResult:
        return retrieve_wiki(db, current_user, question, top_k=top_k)
