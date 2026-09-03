"""Phase 7C.1：Evidence→ApiSourceDocument 适配器（生产 Pipeline 数据入口）。

规则：
1. 只读取属于当前 source Page 的 active EvidenceItem；
2. source_doc_hash 必须与当前 Page 内容 hash 一致，不一致按 stale 处理、不进入编译；
3. 每条 Evidence 映射 evidence_id/source_page_id/source_chunk_id/evidence_type/
   locator/content_hash/status，并生成有界 excerpt（来自 EvidenceItem.content）；
4. locator_json 非法 / Evidence hash 非法 / 跨 Page Evidence：fail closed，返回
   结构化 AdapterIssue，不外泄原始内部异常文本；
5. 只读：不修改 EvidenceItem/Page、不写数据库；
6. format 用确定性规则识别（OpenAPI JSON / OpenAPI YAML / Markdown），不调 LLM；
7. 输入顺序变化不影响输出顺序与序列化结果（页面与 Evidence 均按 id 排序）。

excerpt 只存在于 ApiSourceDocument（输入侧 / 后续 LLM Prompt），不进入
ApiCompileResult / Artifact DTO / notes / diagnostics / 渲染正文。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.core.wiki_skills.api_reference.compiler import ApiSourceDocument

# 对外固定 adapter issue code（不携带原始异常文本）。
ISSUE_MISSING_PAGE = "PAGE_MISSING"
ISSUE_CROSS_PAGE = "EVIDENCE_CROSS_PAGE"
ISSUE_LOCATOR_INVALID = "EVIDENCE_LOCATOR_INVALID"
ISSUE_HASH_INVALID = "EVIDENCE_HASH_INVALID"
ISSUE_SOURCE_HASH_STALE = "SOURCE_HASH_STALE"
ISSUE_EXCERPT_BUDGET_EXCEEDED = "EXCERPT_BUDGET_EXCEEDED"

_HASH64_RE = re.compile(r"^[0-9a-f]{64}$")

# 内容探测只读有界前缀（不整页加载/解析）。
_MAX_FORMAT_PROBE_CHARS = 8000

_JSON_MIME_SUFFIX = ("application/json", ".json")
_YAML_MIME_SUFFIX = ("application/yaml", "application/x-yaml", "text/yaml",
                     ".yaml", ".yml")


@dataclass(frozen=True)
class AdapterIssue:
    """结构化 adapter 诊断（fail closed，不含原始异常/正文/路径）。"""

    code: str
    source_page_id: str = ""
    evidence_id: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "source_page_id": self.source_page_id,
                "evidence_id": self.evidence_id, "message": self.message}


def _hint_kind(source_path: str | None, source_mime_type: str | None) -> str | None:
    """确定性来源信息提示（路径/媒体类型），None=无提示。"""
    path = (source_path or "").lower().strip()
    mime = (source_mime_type or "").lower().strip()
    for suffix in _JSON_MIME_SUFFIX:
        if path.endswith(suffix) or (suffix.startswith(".") is False
                                     and mime.startswith(suffix)):
            return "openapi_json"
    for suffix in _YAML_MIME_SUFFIX:
        if path.endswith(suffix) or (suffix.startswith(".") is False
                                     and mime.startswith(suffix)):
            return "openapi_yaml"
    return None


def detect_source_format(
    content: str,
    *,
    source_path: str | None = None,
    source_mime_type: str | None = None,
    max_probe_chars: int = _MAX_FORMAT_PROBE_CHARS,
) -> str:
    """确定性格式识别：来源信息提示优先，内容探测只读有界前缀（不调 LLM）。

    任何 JSON/YAML 探测异常都安全回落（不崩溃）；最终格式仍由 OpenAPI parser
    完整验证。
    """
    if not isinstance(content, str):
        return "markdown"
    hint = _hint_kind(source_path, source_mime_type)
    if hint is not None:
        return hint

    prefix = content.lstrip("\ufeff \t\r\n")[:max_probe_chars]
    if prefix.startswith("{"):
        try:
            parsed = json.loads(prefix)
            if isinstance(parsed, dict) and isinstance(parsed.get("openapi"), str):
                return "openapi_json"
        except (ValueError, TypeError):
            pass
    try:
        import yaml

        parsed = yaml.safe_load(prefix)
        if isinstance(parsed, dict) and isinstance(parsed.get("openapi"), str):
            return "openapi_yaml"
    except yaml.YAMLError:
        pass
    except (ImportError, ValueError):
        pass
    return "markdown"


def _is_hash64(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASH64_RE.match(value))


def build_api_source_documents_with_diagnostics(
    db,
    pages: Sequence[Any],
    *,
    max_excerpt_chars: int = 2000,
    max_total_excerpt_chars: int = 12000,
) -> tuple[tuple[ApiSourceDocument, ...], tuple[AdapterIssue, ...]]:
    """Page 集合 → 有序 ApiSourceDocument 元组 + 结构化诊断（纯读取）。

    失败以结构化 AdapterIssue 报告并跳过对应项；不抛内部异常（参数错误除外）。
    """
    if max_excerpt_chars < 1 or max_total_excerpt_chars < max_excerpt_chars:
        raise ValueError("invalid excerpt budget")
    from app.models.database import EvidenceItem

    issues: list[AdapterIssue] = []
    # 页面去重并按 id 排序（输入顺序不影响输出）。
    seen: set[str] = set()
    ordered_pages: list[Any] = []
    for page in pages:
        pid = getattr(page, "id", None) if page is not None else None
        if pid is None or not str(pid).strip():
            issues.append(AdapterIssue(code=ISSUE_MISSING_PAGE, message="page without id skipped"))
            continue
        pid = str(pid).strip()
        if pid in seen:
            continue
        seen.add(pid)
        ordered_pages.append((pid, page))
    ordered_pages.sort(key=lambda pair: pair[0])

    documents: list[ApiSourceDocument] = []
    for pid, page in ordered_pages:
        content = str(getattr(page, "content", "") or "")
        page_hash = getattr(page, "content_hash", None)
        if not _is_hash64(page_hash):
            issues.append(AdapterIssue(
                code=ISSUE_SOURCE_HASH_STALE, source_page_id=pid,
                message="page content_hash unavailable/invalid; cannot verify "
                        "evidence (stale)"))
            continue
        rows = db.query(EvidenceItem).filter(
            EvidenceItem.source_page_id == pid).all()
        rows = [r for r in rows if r is not None]
        rows.sort(key=lambda r: str(r.id or ""))

        records: list[dict] = []
        excerpts: list[tuple[str, str]] = []
        for ei in rows:
            eid = str(ei.id or "")
            if not eid:
                issues.append(AdapterIssue(
                    code=ISSUE_HASH_INVALID, source_page_id=pid,
                    message="evidence without id skipped"))
                continue
            if str(ei.source_page_id or "") != pid:
                issues.append(AdapterIssue(
                    code=ISSUE_CROSS_PAGE, source_page_id=pid,
                    evidence_id=eid,
                    message="evidence belongs to another page"))
                continue
            if (ei.status or "") != "active":
                continue  # stale/rejected 排除
            # content_hash：None/空/非小写 64 位 SHA-256 一律拒绝（EVIDENCE_HASH_INVALID）。
            if not _is_hash64(ei.content_hash):
                issues.append(AdapterIssue(
                    code=ISSUE_HASH_INVALID, source_page_id=pid,
                    evidence_id=eid,
                    message="evidence content_hash missing/empty/non-lowercase "
                            "sha256"))
                continue
            if not _is_hash64(ei.source_doc_hash):
                issues.append(AdapterIssue(
                    code=ISSUE_SOURCE_HASH_STALE, source_page_id=pid,
                    evidence_id=eid,
                    message="evidence source_doc_hash invalid/unavailable"))
                continue
            if ei.source_doc_hash != page_hash:
                issues.append(AdapterIssue(
                    code=ISSUE_SOURCE_HASH_STALE, source_page_id=pid,
                    evidence_id=eid,
                    message="evidence source_doc_hash mismatches page content_hash"))
                continue
            try:
                locator = json.loads(ei.locator_json or "{}")
            except (ValueError, TypeError):
                issues.append(AdapterIssue(
                    code=ISSUE_LOCATOR_INVALID, source_page_id=pid,
                    evidence_id=eid, message="locator_json is not valid JSON"))
                continue
            if not isinstance(locator, dict):
                issues.append(AdapterIssue(
                    code=ISSUE_LOCATOR_INVALID, source_page_id=pid,
                    evidence_id=eid, message="locator must be a JSON object"))
                continue
            excerpt = str(ei.content or "")[:max_excerpt_chars]
            records.append({
                "evidence_id": eid,
                "source_page_id": pid,
                "source_chunk_id": ei.source_chunk_id,
                "evidence_type": str(ei.evidence_type or "text"),
                "locator": locator,
                "content_hash": ei.content_hash,
                "status": "active",
            })
            excerpts.append((eid, excerpt))

        if not records:
            continue
        total = sum(len(t) for _e, t in excerpts)
        if total > max_total_excerpt_chars:
            issues.append(AdapterIssue(
                code=ISSUE_EXCERPT_BUDGET_EXCEEDED, source_page_id=pid,
                message=f"active evidence excerpt total {total} exceeds "
                        f"{max_total_excerpt_chars}"))
            continue
        documents.append(ApiSourceDocument(
            source_page_id=pid,
            format=detect_source_format(
                content,
                source_path=getattr(page, "source_path", None),
                source_mime_type=getattr(page, "source_mime_type", None)),
            content=content,
            version_scope="",
            label="",
            evidence=tuple(records),
            excerpts=tuple(excerpts),
        ))
    documents.sort(key=lambda d: d.source_page_id)
    return tuple(documents), tuple(issues)


def build_api_source_documents(
    db,
    pages: Sequence[Any],
    *,
    max_excerpt_chars: int = 2000,
    max_total_excerpt_chars: int = 12000,
) -> tuple[ApiSourceDocument, ...]:
    """只返回文档（happy-path 便捷入口）；诊断用 *_with_diagnostics 获取。"""
    docs, _issues = build_api_source_documents_with_diagnostics(
        db, pages, max_excerpt_chars=max_excerpt_chars,
        max_total_excerpt_chars=max_total_excerpt_chars)
    return docs
