"""Phase 8B：API Reference Section 展示 DTO（display）的构建与只读白名单重建。

契约：本目录 DISPLAY_CONTRACT.md（已冻结）。本模块只做两件事：

1. build_section_display（compile 侧）：对已通过验证的 ApiDocumentIR 的单个
   endpoint role Section 产出确定性展示字典（写入 structure["display"]）；
2. sanitize_section_display / section_api_view（只读侧）：把 storage 的
   structure["display"] 按契约白名单逐字段重建，未知键丢弃、类型不符置空，
   并把“降级规则”收敛为 display=null（正文 hash / 人工保护 / 越界等）。

本模块为纯函数：不 import database / Session / ORM；参数均为标量或内存 DTO。
展示事实只来自 IR 事实字段；绝不复制 evidence_ids / Prompt / 诊断 / 路径 /
ACL / Secret。函数名与输出键以契约 §2/§3/§4 为准。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from app.core.wiki_skills.api_reference.blueprint import SECTION_ROLES

# display 的 schema 版本（契约 §2 冻结值）。v2：参数增加显式 type、
# 请求体/响应逐媒体类型保留并给出保守 schema_status（present/unspecified）。
API_SECTION_DISPLAY_SCHEMA = "api-section-display/v2"

# 展示边界：序列化字节上限与嵌套深度上限（超出 → display=null，绝不截断冒充完整）。
DISPLAY_MAX_BYTES = 100_000
DISPLAY_MAX_DEPTH = 12

# structure_json 读取边界：先做廉价字符长度上限，再在有限输入上确认 UTF-8 字节数；
# 超限直接安全降级（不解析巨大的 JSON，不把此修正扩成通用 JSON 框架）。
STRUCTURE_JSON_MAX_CHARS = 300_000
STRUCTURE_JSON_MAX_BYTES = 500_000

# 逐媒体类型的 schema 保守语义：无法区分“缺失 Schema”与“显式空 Schema”时，
# 一律标为 unspecified（UI 用“未提供具体结构”，不宣称“无 Schema”）。
SCHEMA_STATUS_VALUES = ("present", "unspecified")

# 顶层 structure 里合法的 section_role 集合（read 侧 role 只认这个集合）。
KNOWN_SECTION_ROLES: frozenset[str] = frozenset(SECTION_ROLES)

_HASH64_RE = re.compile(r"^[0-9a-f]{64}$")

# display 顶层键（白名单：多一个键都不允许进入重建后的 DTO）。
_DISPLAY_KEYS = (
    "schema_version",
    "content_hash",
    "section_role",
    "version_scope",
    "endpoint",
    "parameters",
    "request_body",
    "responses",
    "error_codes",
    "examples",
    "version_notes",
    "knowledge_gaps",
    "conflicts",
)


def content_hash_of(content: str) -> str:
    """与 v3 写库共用同一 hash 函数：sha256((content or "").encode("utf-8"))。"""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    """把不可变 IR 容器（MappingProxy/tuple）转为普通 dict/list（JSON-safe）。"""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _json_roundtrip(value: Any) -> Any:
    """json.dumps/loads 往返：验证 value 是严格 JSON-safe 并归一化为 JSON 原生结构。"""
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("not json-safe")
    try:
        return json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("not json-safe")


def _depth_of(value: Any) -> int:
    """非递归嵌套深度：顶层容器（dict/list）计 1，逐层 +1；标量为 0。

    深度 > DISPLAY_MAX_DEPTH 即视为超限（契约 §3：绝不静默截断冒充完整）。
    """
    max_depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            max_depth = depth
        if isinstance(node, Mapping):
            for child in node.values():
                if isinstance(child, (Mapping, list, tuple)):
                    stack.append((child, depth + 1))
        elif isinstance(node, (list, tuple)):
            for child in node:
                if isinstance(child, (Mapping, list, tuple)):
                    stack.append((child, depth + 1))
    return max_depth


def _within_bounds(obj: Any) -> bool:
    """深度 / 字节双边界（先查深度避免对病态深 JSON 做序列化）。"""
    if _depth_of(obj) > DISPLAY_MAX_DEPTH:
        return False
    try:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return len(raw.encode("utf-8")) <= DISPLAY_MAX_BYTES


# ---------------------------------------------------------------------------
# compile 侧投影
# ---------------------------------------------------------------------------


def _declared_type(schema: Any) -> str:
    """只读 schema.type 的显式声明：字符串或类型数组；其余一律 ''（UI 显示未提供）。

    绝不根据 name / example / format / 默认值猜测类型。schema 非 dict、
    type 缺失、type 非 str 且非全字符串数组、数组为空 → ''。
    """
    if not isinstance(schema, Mapping):
        return ""
    raw = schema.get("type")
    if isinstance(raw, str):
        return raw.strip()
    # 冻结容器会把列表转 tuple；str/非全字符串项忽略。
    if isinstance(raw, (list, tuple)):
        parts = [str(t).strip() for t in raw if isinstance(t, str) and t.strip()]
        return " | ".join(parts)
    return ""


def _param_item(param) -> dict:
    """单个参数 → 展示项。location 以 DTO 字段为准（path/query/header）。"""
    return {
        "location": param.location,
        "name": param.name,
        "required": bool(param.required),
        "description": param.description or "",
        # Phase 8B.1：只读 schema.type 显式类型；未声明 → ''（不推测）。
        "type": _declared_type(getattr(param, "schema", None)),
    }


def _media_items(content: Any) -> list[dict]:
    """{media_type: schema-dict} → 展示列表（保留每个媒体类型名称）。

    schema_status：schema dict 非空 → present；为空/无法区分缺失与显式空 →
    unspecified（UI 用保守文案，不宣称“无 Schema”）。
    """
    items = []
    for media_type in sorted((content or {}).keys()):
        schema = (content or {}).get(media_type)
        # IR 冻结容器为 MappingProxyType（不是 dict），统一按 Mapping 判断。
        present = isinstance(schema, Mapping) and bool(schema)
        items.append({
            "media_type": str(media_type),
            "schema_status": "present" if present else "unspecified",
        })
    return items


def build_section_display(ir, spec, content_hash: str) -> dict | None:
    """endpoint role Section → display 字典；任何失败/越界返回 None。

    仅当 spec.section_role == "endpoint" 且 ir.endpoints 中存在 endpoint_id 精确
    匹配的唯一 endpoint 时尝试。输出只含 IR 事实字段的确定性投影。
    """
    try:
        if getattr(spec, "section_role", None) != "endpoint":
            return None
        target_id = (getattr(spec, "endpoint_id", "") or "").strip()
        if not target_id:
            return None
        matches = [ep for ep in (ir.endpoints or ())
                   if getattr(ep, "endpoint_id", "") == target_id]
        if len(matches) != 1:
            return None
        ep = matches[0]

        parameters = []
        for group in (getattr(ep, "path_parameters", ()) or (),
                      getattr(ep, "query_parameters", ()) or (),
                      getattr(ep, "headers", ()) or ()):
            for param in group:
                parameters.append(_param_item(param))

        request_body = None
        rb = getattr(ep, "request_body", None)
        if rb is not None:
            request_body = {
                "required": bool(getattr(rb, "required", False)),
                "description": getattr(rb, "description", "") or "",
                "media_types": _media_items(getattr(rb, "content", None)),
            }

        responses = sorted(
            ({"status_code": r.status_code,
              "description": r.description or "",
              "media_types": _media_items(getattr(r, "content", None))}
             for r in (getattr(ep, "responses", ()) or ())),
            key=lambda item: item["status_code"],
        )
        error_codes = sorted(
            ({"code": e.code, "description": e.description or "",
              "http_status": (e.http_status or "")}
             for e in (getattr(ep, "error_codes", ()) or ())),
            key=lambda item: (item["code"], item["http_status"]),
        )
        examples = sorted(
            ({"title": e.title, "description": e.description or "",
              "media_type": e.media_type or "",
              "content": _plain(getattr(e, "content", None))}
             for e in (getattr(ep, "examples", ()) or ())),
            key=lambda item: item["title"],
        )

        version_notes = sorted(
            ({"version_scope": n.version_scope or "",
              "note": n.note or ""}
             for n in (ir.version_notes or ())
             if getattr(n, "endpoint_id", "") == target_id),
            key=lambda item: (item["version_scope"], item["note"]),
        )
        knowledge_gaps = sorted(
            ({"gap_type": g.gap_type, "description": g.description or ""}
             for g in (ir.knowledge_gaps or ())
             if getattr(g, "endpoint_id", "") == target_id),
            key=lambda item: (item["gap_type"], item["description"]),
        )
        conflict_paths = sorted({
            b.field_path for b in (getattr(ep, "evidence_bindings", ()) or ())
            if getattr(b, "usage_type", "") == "conflict"
            and getattr(b, "field_path", "")
        })
        conflicts = [{"field_path": p} for p in conflict_paths]

        display = {
            "schema_version": API_SECTION_DISPLAY_SCHEMA,
            "content_hash": content_hash,
            "section_role": "endpoint",
            "version_scope": getattr(ep, "version_scope", "") or "",
            "endpoint": {
                "method": getattr(ep, "method", "") or "",
                "path": getattr(ep, "path", "") or "",
                "summary": getattr(ep, "summary", "") or "",
                "description": getattr(ep, "description", "") or "",
            },
            "parameters": parameters,
            "request_body": request_body,
            "responses": responses,
            "error_codes": error_codes,
            "examples": examples,
            "version_notes": version_notes,
            "knowledge_gaps": knowledge_gaps,
            "conflicts": conflicts,
        }
        if not _within_bounds(display):
            return None
        return display
    except Exception:  # noqa: BLE001 - 契约：任何异常不产出展示
        return None


# ---------------------------------------------------------------------------
# 只读侧白名单重建
# ---------------------------------------------------------------------------


def _need_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    return value


def _need_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a real bool")
    return value


def _sanitize_parameters(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise TypeError("parameters must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("parameters item must be an object")
        location = _need_str(item.get("location"), "parameters.location")
        if location not in ("path", "query", "header"):
            raise TypeError("parameters.location must be path/query/header")
        out.append({
            "location": location,
            "name": _need_str(item.get("name"), "parameters.name"),
            "required": _need_bool(item.get("required"), "parameters.required"),
            "description": _need_str(item.get("description"),
                                     "parameters.description"),
            "type": _need_str(item.get("type"), "parameters.type"),
        })
    return out


def _sanitize_media_items(raw: Any, ctx: str) -> list[dict]:
    """media_types 白名单：media_type 字符串 + schema_status ∈ present/unspecified。

    仅保留这两个键；旧版 schema_present 布尔语义已废弃（v2）。
    """
    if not isinstance(raw, list):
        raise TypeError(f"{ctx} must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError(f"{ctx} item must be an object")
        status = _need_str(item.get("schema_status"), f"{ctx}.schema_status")
        if status not in SCHEMA_STATUS_VALUES:
            raise TypeError(f"{ctx}.schema_status must be present/unspecified")
        out.append({
            "media_type": _need_str(item.get("media_type"), f"{ctx}.media_type"),
            "schema_status": status,
        })
    return out


def _sanitize_request_body(raw: Any) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("request_body must be object or null")
    return {
        "required": _need_bool(raw.get("required"), "request_body.required"),
        "description": _need_str(raw.get("description"),
                                 "request_body.description"),
        "media_types": _sanitize_media_items(
            raw.get("media_types"), "request_body.media_types"),
    }


def _sanitize_responses(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise TypeError("responses must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("responses item must be an object")
        out.append({
            "status_code": _need_str(item.get("status_code"),
                                     "responses.status_code"),
            "description": _need_str(item.get("description"),
                                     "responses.description"),
            "media_types": _sanitize_media_items(
                item.get("media_types"), "responses.media_types"),
        })
    return out


def _sanitize_error_codes(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise TypeError("error_codes must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("error_codes item must be an object")
        out.append({
            "code": _need_str(item.get("code"), "error_codes.code"),
            "description": _need_str(item.get("description"),
                                     "error_codes.description"),
            "http_status": _need_str(item.get("http_status"),
                                     "error_codes.http_status"),
        })
    return out


def _sanitize_examples(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise TypeError("examples must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("examples item must be an object")
        content = _json_roundtrip(item.get("content"))
        out.append({
            "title": _need_str(item.get("title"), "examples.title"),
            "description": _need_str(item.get("description"),
                                     "examples.description"),
            "media_type": _need_str(item.get("media_type"),
                                    "examples.media_type"),
            "content": content,
        })
    return out


def _sanitize_pairs(raw: Any, key: str, sub_keys: tuple[str, ...]) -> list[dict]:
    if not isinstance(raw, list):
        raise TypeError(f"{key} must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError(f"{key} item must be an object")
        out.append({sub: _need_str(item.get(sub), f"{key}.{sub}")
                    for sub in sub_keys})
    return out


def _sanitize_conflicts(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise TypeError("conflicts must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("conflicts item must be an object")
        out.append({"field_path": _need_str(item.get("field_path"),
                                            "conflicts.field_path")})
    return out


def sanitize_section_display(candidate) -> dict | None:
    """严格白名单重建（契约 §3）。候选非 dict / 版本不符 / role 非 endpoint /
    hash 非 64 小写 hex / 任一字段类型不符 / 越界 / 含非 JSON-safe 值 → None。
    未知键一律丢弃；重建对象不含任何契约外键。"""
    try:
        if not isinstance(candidate, dict):
            return None
        if candidate.get("schema_version") != API_SECTION_DISPLAY_SCHEMA:
            return None
        if candidate.get("section_role") != "endpoint":
            return None
        content_hash = _need_str(candidate.get("content_hash"), "content_hash")
        if not _HASH64_RE.match(content_hash):
            return None

        endpoint_raw = candidate.get("endpoint")
        if not isinstance(endpoint_raw, dict):
            return None
        endpoint = {
            "method": _need_str(endpoint_raw.get("method"), "endpoint.method"),
            "path": _need_str(endpoint_raw.get("path"), "endpoint.path"),
            "summary": _need_str(endpoint_raw.get("summary"),
                                 "endpoint.summary"),
            "description": _need_str(endpoint_raw.get("description"),
                                     "endpoint.description"),
        }

        display = {
            "schema_version": API_SECTION_DISPLAY_SCHEMA,
            "content_hash": content_hash,
            "section_role": "endpoint",
            "version_scope": _need_str(candidate.get("version_scope"),
                                       "version_scope"),
            "endpoint": endpoint,
            "parameters": _sanitize_parameters(candidate.get("parameters")),
            "request_body": _sanitize_request_body(candidate.get("request_body")),
            "responses": _sanitize_responses(candidate.get("responses")),
            "error_codes": _sanitize_error_codes(candidate.get("error_codes")),
            "examples": _sanitize_examples(candidate.get("examples")),
            "version_notes": _sanitize_pairs(
                candidate.get("version_notes"), "version_notes",
                ("version_scope", "note")),
            "knowledge_gaps": _sanitize_pairs(
                candidate.get("knowledge_gaps"), "knowledge_gaps",
                ("gap_type", "description")),
            "conflicts": _sanitize_conflicts(candidate.get("conflicts")),
        }
        if set(display) != set(_DISPLAY_KEYS):
            return None
        if not _within_bounds(display):
            return None
        return display
    except Exception:  # noqa: BLE001 - 契约：任何异常视为不可信
        return None


# ---------------------------------------------------------------------------
# 只读侧 section 级视图（纯函数；供 wiki API _sections_payload 使用）
# ---------------------------------------------------------------------------


def section_api_view(
    structure_json: str | None,
    content: str,
    *,
    content_origin=None,
    merge_policy=None,
    locked=None,
    validation_status=None,
) -> tuple[str | None, dict | None]:
    """解析一条 Section 的可展示视图：(section_role, display)。

    - role：只从 structure_json 顶层 section_role 解析（合法集合才返回）；
      structure 缺失 / 非 JSON / 顶层非对象 / 未知 role → section_role=None。
    - display：契约 §4 降级规则全部通过才返回；任一命中 → None（role 仍可返回）。
    - 纯函数：不读库、不 import database；参数均为标量。
    """
    role: str | None = None
    parsed: dict | None = None
    if structure_json:
        # 读取边界：先廉价字符长度上限，再在有限输入上确认 UTF-8 字节数；
        # 超限直接安全降级，不完整解析巨大的 JSON。
        if len(structure_json) > STRUCTURE_JSON_MAX_CHARS:
            return (None, None)
        try:
            if len(structure_json.encode("utf-8")) > STRUCTURE_JSON_MAX_BYTES:
                return (None, None)
        except (UnicodeEncodeError, UnicodeDecodeError):
            return (None, None)
        try:
            loaded = json.loads(structure_json)
        except Exception:  # noqa: BLE001
            return (None, None)
        if isinstance(loaded, dict):
            parsed = loaded
            raw_role = loaded.get("section_role")
            if isinstance(raw_role, str) and raw_role in KNOWN_SECTION_ROLES:
                role = raw_role
    if role is None:
        return (role, None)

    display: dict | None = None
    if role == "endpoint" and validation_status == "pass":
        if content_origin == "manual" or merge_policy == "protected" or locked:
            return (role, None)
        display_raw = parsed.get("display") if isinstance(parsed, dict) else None
        if isinstance(display_raw, dict):
            cleaned = sanitize_section_display(display_raw)
            if cleaned is not None and \
                    content_hash_of(content) == cleaned.get("content_hash"):
                display = cleaned
    return (role, display)
