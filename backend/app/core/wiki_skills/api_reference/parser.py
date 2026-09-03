"""Phase 7A：确定性解析器（OpenAPI 3 JSON/YAML + Markdown hint）。

规则：
- 只处理确定性、可验证输入；不调 LLM，不猜测字段类型/required/Schema；
- 输入大小设置上限；JSON/YAML 根必须为 Mapping；openapi 字段必须存在且主版本为 3；
  paths 必须为 Mapping；
- HTTP method/path/parameters/requestBody/responses/security/components/schemas 由
  结构化字段确定性读取；HTTP method、path、参数名、状态码、错误码不被改写；
- `$ref` 只支持本地 `#/components/...`：设最大解析深度、检测循环、外部 ref 拒绝
  （记录 gap，不下载任何内容）；
- 每个解析出的事实必须绑定输入 Evidence ID（evidence.py active 过滤）；
  无匹配 Evidence 的内容只能进入 knowledge_gaps，不成为事实字段；
- 非法状态码 / 参数位置 / method 产生安全诊断（knowledge gap），不让整批崩溃；
  根级结构违法（非 Mapping / 缺 openapi / 版本不支持）抛 ApiReferenceParseError；
- Markdown：只识别明确写出的 method+path / 状态码候选，产出 hint 型 gap；
  不从自然语言猜参数类型、required、Schema。

本模块为纯函数 + 只读输入：不写 notes.db、不 import 生产 Pipeline、
不触碰 production Registry / builtin Skill。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

from app.core.wiki_skills.api_reference.evidence import build_evidence_ref
from app.core.wiki_skills.api_reference.identity import (
    HTTP_METHODS,
    normalize_api_path,
    normalize_http_method,
    normalize_version_scope,
)
from app.core.wiki_skills.api_reference.schemas import (
    PARAM_LOCATION_VALUES,
    ApiAuthentication,
    ApiDataModel,
    ApiDocumentIR,
    ApiEndpoint,
    ApiErrorCode,
    ApiExample,
    ApiFieldBinding,
    ApiKnowledgeGap,
    ApiParameter,
    ApiRequestBody,
    ApiResponse,
    assert_json_safe,
)

# 输入上限与解析深度。
MAX_OPENAPI_BYTES = 2_000_000
MAX_MARKDOWN_BYTES = 512_000
MAX_REF_DEPTH = 16
_MAX_TEXT_LEN = 4_000_000

# OpenAPI 响应状态码：default / 3 位数字 / 1XX-5XX 范围。
_STATUS_RE = re.compile(r"^(?:default|[1-5][0-9]{2}|[1-5]XX)$")

_METHOD_TOKENS = tuple(m.lower() for m in HTTP_METHODS)


class ApiReferenceParseError(ValueError):
    """根级解析失败（结构性违法；items 级问题走 knowledge_gaps 诊断）。"""


def _safe_token(value: Any, default: str = "item") -> str:
    """把任意 label 清洗成 field_path 安全 token（不改写原值语义，仅转义）。"""
    text = str(value)
    token = re.sub(r"[^A-Za-z0-9_.\-]", "_", text)
    token = re.sub(r"_+", "_", token).strip("_")
    return token or default


# ---------------------------------------------------------------------------
# 文档装载（JSON / YAML，大小上限，根必须 Mapping）
# ---------------------------------------------------------------------------


def _load_document(text: str, fmt: str | None, max_bytes: int, source: str) -> dict:
    if not isinstance(text, str):
        raise ApiReferenceParseError(f"{source}: input must be str text")
    data_bytes = text.encode("utf-8")
    if len(data_bytes) > max_bytes:
        raise ApiReferenceParseError(
            f"{source}: input exceeds size limit {max_bytes} bytes")
    if len(text) > _MAX_TEXT_LEN:
        raise ApiReferenceParseError(f"{source}: input text too long")
    if fmt not in (None, "json", "yaml"):
        raise ApiReferenceParseError(f"{source}: unsupported format {fmt!r}")
    stripped = text.lstrip("\ufeff \t\r\n")

    data: Any = None
    want_json = fmt == "json" or (fmt is None and stripped.startswith("{"))
    if want_json:
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if fmt == "json":
                raise ApiReferenceParseError(f"{source}: invalid JSON: {exc}") from exc
            # auto 模式：JSON 失败回退 YAML（JSON 是合法 YAML 子集防御）。
    if data is None:
        try:
            import yaml
        except ImportError as exc:
            raise ApiReferenceParseError(
                f"{source}: PyYAML unavailable (cannot parse YAML input)") from exc
        try:
            data = yaml.safe_load(stripped)
        except yaml.YAMLError as exc:
            raise ApiReferenceParseError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ApiReferenceParseError(f"{source}: document root must be a mapping")
    return data


def _clean(value: Any) -> Any:
    """清洗为 JSON-safe 普通结构；含 bytes/NaN/对象时抛 ValueError。"""
    assert_json_safe(value, "content")
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Evidence 匹配（纯确定性；归一化后比较，不改写）
# ---------------------------------------------------------------------------


def _evidence_section(ref) -> str:
    loc = ref.locator
    if isinstance(loc, Mapping):
        section = loc.get("section")
        if isinstance(section, str):
            return section.strip()
    return "endpoints"


def _matches_operation(ref, method: str, path: str) -> bool:
    loc = ref.locator
    if not isinstance(loc, Mapping):
        return False
    section = _evidence_section(ref)
    if section == "all":
        return True
    if section not in ("endpoints", ""):
        return False
    ev_method = loc.get("method")
    ev_path = loc.get("path")
    if ev_method is None or ev_path is None:
        return False
    try:
        return (
            normalize_http_method(ev_method) == method
            and normalize_api_path(ev_path) == path
        )
    except ValueError:
        return False


def _matches_named(ref, section: str, name: str) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if _evidence_section(ref) == "all":
        return True
    if _evidence_section(ref) != section:
        return False
    return str(ref.locator.get("name", "")) == name


def _matches_overview(ref) -> bool:
    return _evidence_section(ref) in ("all", "overview")


def _matching_ids(refs: tuple, predicate) -> tuple[str, ...]:
    return tuple(sorted({r.evidence_id for r in refs if predicate(r)}))


# ---------------------------------------------------------------------------
# $ref 审计（本地 / 有界 / 循环检测 / 外部拒绝，不下载）
# ---------------------------------------------------------------------------


def _resolve_local_pointer(pointer: str, document: dict):
    if not pointer.startswith("#/"):
        raise ApiReferenceParseError("non-local ref")
    parts = pointer[2:].split("/")
    node: Any = document
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise ApiReferenceParseError("unresolvable local ref")
        node = node[part]
    return node


class _RefResolutionError(Exception):
    """受控 ref 解析失败（kind 决定 knowledge gap 类型，不让整份文档崩溃）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _classify_ref_kind(ref: str) -> str:
    return "external_ref" if "://" in ref else "unresolvable_ref"


def _resolve_ref_chain(document: dict, node: Any) -> Any:
    """有界链式解析本地 `#/components/...` 引用至「无顶层 $ref」的目标。

    规则：
    - 每次跳转必须指向 `#/components/...`：外部 URL / 文件路径 / 非法 pointer
      一律拒绝，不访问网络或文件系统；
    - visited 引用集合检测循环（含别名 A→B→A）；
    - 跳转次数超过 MAX_REF_DEPTH 报 max_depth_ref；
    - 不实现完整 OpenAPI resolver，不处理跨文件引用。
    最终返回不含顶层 `$ref` 的目标（JSON-safe Mapping）。
    """
    visited: list[str] = []
    current = node
    depth = 0
    while True:
        if depth > MAX_REF_DEPTH:
            raise _RefResolutionError(
                "max_depth_ref",
                f"local ref chain exceeds max depth {MAX_REF_DEPTH}")
        if not isinstance(current, dict) or not isinstance(current.get("$ref"), str):
            return current
        ref = current["$ref"]
        if not ref.startswith("#/components/"):
            raise _RefResolutionError(
                _classify_ref_kind(ref),
                f"non-components ref rejected (no download): {ref}")
        if ref in visited:
            chain = " -> ".join(visited + [ref])
            raise _RefResolutionError(
                "circular_ref", f"circular $ref detected: {chain}")
        visited.append(ref)
        try:
            target = _resolve_local_pointer(ref, document)
        except ApiReferenceParseError as exc:
            raise _RefResolutionError("unresolvable_ref", str(exc)) from exc
        if not isinstance(target, dict):
            raise _RefResolutionError(
                "unresolvable_ref", f"ref target not a mapping: {ref}")
        current = target
        depth += 1


def _resolve_local_ref(document: dict, node: Any,
                       gaps: list[ApiKnowledgeGap], scope: str) -> Any:
    """参数 / 响应 / Schema 审计共用的受控 ref 入口（同一规则）。

    链式解析最终目标；失败写确定性 knowledge gap 并返回 None（不崩溃）。
    """
    if not isinstance(node, dict) or not isinstance(node.get("$ref"), str):
        return node
    try:
        return _resolve_ref_chain(document, node)
    except _RefResolutionError as exc:
        gaps.append(ApiKnowledgeGap(
            gap_type=exc.kind,
            description=f"{exc.message} ({scope})"))
        return None


def _audit_ref_tree(node, document: dict, gaps: list[ApiKnowledgeGap],
                    scope: str) -> None:
    """有界遍历 schema 树，统一走 _resolve_local_ref（链式 / 有界 / 不下载）。

    审计不展开、不存储被引用对象；外部 / 循环 / 超深 / 不可解析只产出 gap，
    不让整份文档崩溃。分支内引用路径（seen）负责结构性自引用检测。
    """
    def walk(value: Any, depth: int, seen: list[str]) -> None:
        if depth > MAX_REF_DEPTH:
            gaps.append(ApiKnowledgeGap(
                gap_type="max_depth_ref",
                description=f"ref nesting exceeds {MAX_REF_DEPTH} in {scope}"))
            return
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str):
                if ref in seen:
                    chain = " -> ".join(seen + [ref])
                    gaps.append(ApiKnowledgeGap(
                        gap_type="circular_ref",
                        description=f"circular $ref detected: {chain} ({scope})"))
                    return
                resolved = _resolve_local_ref(document, value, gaps, scope)
                if resolved is not None:
                    walk(resolved, depth + 1, seen + [ref])
                return
            for sub in value.values():
                walk(sub, depth, seen)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth, seen)

    walk(node, 0, [])


# ---------------------------------------------------------------------------
# OpenAPI 组件读取
# ---------------------------------------------------------------------------


def _read_security_schemes(document: dict) -> list[ApiAuthentication]:
    result = []
    try:
        schemes = document["components"]["securitySchemes"]
    except (KeyError, TypeError):
        return result
    if not isinstance(schemes, dict):
        return result
    for name in sorted(schemes):
        scheme = schemes[name]
        if not isinstance(scheme, dict):
            continue
        result.append(ApiAuthentication(
            name=str(name),
            description=str(scheme.get("description", "")).strip(),
            kind=str(scheme.get("type", "")).strip(),
            scheme=str(scheme.get("scheme", "")).strip(),
            location=str(scheme.get("in", "")).strip(),
            param_name=str(scheme.get("name", "")).strip(),
        ))
    return result


def _read_data_models(document: dict, refs, gaps: list[ApiKnowledgeGap],
                      ) -> tuple[list[ApiDataModel], list[ApiFieldBinding]]:
    try:
        schemas = document["components"]["schemas"]
    except (KeyError, TypeError):
        return [], []
    if not isinstance(schemas, dict):
        return [], []
    models: list[ApiDataModel] = []
    bindings: list[ApiFieldBinding] = []
    for name in sorted(schemas):
        node = schemas[name]
        if not isinstance(node, dict):
            gaps.append(ApiKnowledgeGap(
                gap_type="unresolvable_ref",
                description=f"schema {name} is not an object (skipped)"))
            continue
        try:
            clean = _clean(node)
        except ValueError as exc:
            gaps.append(ApiKnowledgeGap(
                gap_type="unresolvable_ref",
                description=f"schema {name} not JSON-safe: {exc}"))
            continue
        ids = _matching_ids(refs, lambda r, n=name: _matches_named(r, "data_models", n))
        if not ids:
            gaps.append(ApiKnowledgeGap(
                gap_type="no_evidence",
                description=f"data model {name} has no matching active evidence; "
                            f"not recorded as fact"))
            continue
        _audit_ref_tree(node, document, gaps, f"data_models.{name}")
        models.append(ApiDataModel(
            name=name,
            description=str(node.get("description", "")).strip(),
            schema=clean,
        ))
        bindings.append(ApiFieldBinding(
            field_path=f"data_models.{name}", evidence_ids=ids))
    return models, bindings


# ---------------------------------------------------------------------------
# Operation 级读取
# ---------------------------------------------------------------------------


def _parameter_from_node(node: dict, document: dict,
                         gaps: list[ApiKnowledgeGap], scope: str,
                         ) -> ApiParameter | None:
    location = str(node.get("in", "")).strip().lower()
    if location not in PARAM_LOCATION_VALUES:
        gaps.append(ApiKnowledgeGap(
            gap_type="unsupported_param_location",
            description=f"parameter {node.get('name', '?')} in {scope} has invalid "
                        f"location {node.get('in')!r} (skipped)"))
        return None
    if location == "cookie":
        gaps.append(ApiKnowledgeGap(
            gap_type="unsupported_param_location",
            description=f"cookie parameter {node.get('name', '?')} in {scope} is "
                        f"not supported by IR (skipped)"))
        return None
    raw_schema = node.get("schema")
    if isinstance(raw_schema, dict):
        _audit_ref_tree(raw_schema, document, gaps, f"{scope}.param")
    try:
        schema = _clean(raw_schema or {})
    except ValueError as exc:
        gaps.append(ApiKnowledgeGap(
            gap_type="unresolvable_ref",
            description=f"parameter schema in {scope} not JSON-safe: {exc}"))
        schema = {}
    if not isinstance(schema, dict):
        schema = {}
    return ApiParameter(
        name=str(node.get("name", "")).strip(),
        location=location,
        required=bool(node.get("required", False)),
        description=str(node.get("description", "")).strip(),
        schema=schema,
    )


def _parse_parameter_groups(params: Any, document: dict,
                            gaps: list[ApiKnowledgeGap],
                            scope: str) -> dict[str, list[ApiParameter]]:
    """解析一组 parameters（含本地 `$ref`）为 {location: [ApiParameter]}。

    参数身份为 (location, name)；同列表内同身份重复项跳过并记 gap。
    cookie 与非法位置在 _parameter_from_node 阶段拒绝（安全诊断）。
    """
    groups: dict[str, list[ApiParameter]] = {
        "path": [], "query": [], "header": [],
    }
    seen: set[tuple[str, str]] = set()
    if not isinstance(params, list):
        return groups
    for item in params:
        resolved = _resolve_local_ref(document, item, gaps, scope)
        if resolved is None or not isinstance(resolved, dict):
            continue
        param = _parameter_from_node(resolved, document, gaps, scope)
        if param is None or param.location not in groups:
            continue
        identity = (param.location, param.name)
        if identity in seen:
            gaps.append(ApiKnowledgeGap(
                gap_type="ambiguous_input",
                description=f"duplicate parameter {identity} in {scope} "
                            f"(skipped duplicate)"))
            continue
        seen.add(identity)
        groups[param.location].append(param)
    return groups


def _merge_parameter_groups(path_level: dict[str, list[ApiParameter]],
                            operation_level: dict[str, list[ApiParameter]],
                            ) -> dict[str, list[ApiParameter]]:
    """path-level 与 operation-level 参数确定性合并。

    - 身份 = (location, name)；operation-level 同身份覆盖 path-level；
    - 不同参数（含同名不同 location，如 query/header 同名）保留合并；
    - 结果与输入列表顺序无关（按身份索引重建，path 优先、op 覆盖/追加）。
    """
    merged: dict[str, list[ApiParameter]] = {}
    for location in ("path", "query", "header"):
        index: dict[tuple[str, str], ApiParameter] = {}
        for param in path_level.get(location, ()):
            index[(param.location, param.name)] = param
        for param in operation_level.get(location, ()):
            index[(param.location, param.name)] = param  # operation 覆盖
        merged[location] = list(index.values())
    return merged


def _schema_content(content: Any, document: dict, gaps: list[ApiKnowledgeGap],
                    scope: str) -> dict:
    """content → {media_type: sanitized schema}（只含 schema，不含示例）。"""
    result: dict[str, Any] = {}
    if not isinstance(content, dict):
        return result
    for media_type in sorted(content):
        media = content[media_type]
        if not isinstance(media, dict):
            continue
        raw_schema = media.get("schema")
        if isinstance(raw_schema, dict):
            _audit_ref_tree(raw_schema, document, gaps, f"{scope}.{media_type}")
        try:
            schema = _clean(raw_schema or {})
        except ValueError as exc:
            gaps.append(ApiKnowledgeGap(
                gap_type="unresolvable_ref",
                description=f"schema in {scope} {media_type} not JSON-safe: {exc}"))
            schema = {}
        if isinstance(schema, dict):
            result[str(media_type)] = schema
    return result


def _extract_examples(content: Any, document: dict, gaps: list[ApiKnowledgeGap],
                      scope: str, status_code: str) -> list[ApiExample]:
    """从响应 content 提取确定性示例（media example / examples[].value）。"""
    examples: list[ApiExample] = []
    if not isinstance(content, dict):
        return examples
    for media_type in sorted(content):
        media = content[media_type]
        if not isinstance(media, dict):
            continue
        media_token = _safe_token(media_type)
        inline = media.get("example")
        if inline is not None:
            try:
                assert_json_safe(inline, "example")
                examples.append(ApiExample(
                    title=f"example-{status_code}-{media_token}",
                    media_type=str(media_type),
                    content=_clean(inline)))
            except ValueError as exc:
                gaps.append(ApiKnowledgeGap(
                    gap_type="unresolvable_ref",
                    description=f"example in {scope} {media_type} is not "
                                f"JSON-serializable: {exc}"))
        named = media.get("examples")
        if isinstance(named, dict):
            for name in sorted(named):
                entry = named[name]
                if not isinstance(entry, dict):
                    continue
                if "externalValue" in entry and "value" not in entry:
                    gaps.append(ApiKnowledgeGap(
                        gap_type="external_ref",
                        description=f"example {name} in {scope} uses externalValue "
                                    f"(not downloaded)"))
                    continue
                value = entry.get("value")
                try:
                    assert_json_safe(value, "example")
                    examples.append(ApiExample(
                        title=f"example-{status_code}-{_safe_token(name)}",
                        media_type=str(media_type),
                        description=str(entry.get("summary", "")).strip(),
                        content=_clean(value)))
                except ValueError as exc:
                    gaps.append(ApiKnowledgeGap(
                        gap_type="unresolvable_ref",
                        description=f"example {name} in {scope} is not "
                                    f"JSON-serializable: {exc}"))
    return examples


def _status_valid(code: str) -> bool:
    return bool(_STATUS_RE.match(code.strip()))


def _read_response(resolved: dict, code: str, document: dict,
                   gaps: list[ApiKnowledgeGap], scope: str,
                   ) -> ApiResponse | None:
    code = code.strip()
    if not _status_valid(code):
        gaps.append(ApiKnowledgeGap(
            gap_type="invalid_status",
            description=f"invalid response status code {code!r} in {scope} (skipped)"))
        return None
    content_map = _schema_content(resolved.get("content"), document, gaps,
                                  f"{scope}.{code}")
    headers: dict[str, Any] = {}
    raw_headers = resolved.get("headers")
    if isinstance(raw_headers, dict):
        for hname in sorted(raw_headers):
            hnode = raw_headers[hname]
            if isinstance(hnode, dict) and isinstance(hnode.get("schema"), dict):
                try:
                    headers[str(hname)] = _clean(hnode["schema"])
                except ValueError:
                    headers[str(hname)] = {}
    return ApiResponse(
        status_code=code,
        description=str(resolved.get("description", "")).strip(),
        headers=headers,
        content=content_map,
    )


def _build_endpoint(operation: dict, method: str, path: str,
                    document: dict, refs, gaps: list[ApiKnowledgeGap],
                    version_scope: str,
                    path_level_groups: dict[str, list[ApiParameter]],
                    ) -> ApiEndpoint | None:
    from app.core.wiki_skills.api_reference.identity import build_endpoint_id
    eid = build_endpoint_id(method, path, version_scope)
    ids = _matching_ids(refs, lambda r, m=method, p=path:
                        _matches_operation(r, m, p))
    if not ids:
        gaps.append(ApiKnowledgeGap(
            gap_type="no_evidence",
            description=f"candidate endpoint {eid} has no matching active "
                        f"evidence; skipped as fact",
            endpoint_id=eid))
        return None

    group_scope = f"{method} {path} [{version_scope}]"
    op_groups = _parse_parameter_groups(
        operation.get("parameters"), document, gaps, group_scope)
    groups = _merge_parameter_groups(path_level_groups, op_groups)

    summary = str(operation.get("summary", "")).strip()
    description = str(operation.get("description", "")).strip()

    request_body = None
    body = operation.get("requestBody")
    if isinstance(body, dict):
        content_map = _schema_content(body.get("content"), document, gaps,
                                      f"{group_scope}.requestBody")
        request_body = ApiRequestBody(
            description=str(body.get("description", "")).strip(),
            required=bool(body.get("required", False)),
            content=content_map,
        )
    elif body is not None:
        gaps.append(ApiKnowledgeGap(
            gap_type="unresolvable_ref",
            description=f"requestBody in {group_scope} is not an object (skipped)"))

    responses: list[ApiResponse] = []
    examples: list[ApiExample] = []
    error_codes: list[ApiErrorCode] = []
    responses_raw = operation.get("responses")
    if isinstance(responses_raw, dict):
        for code in sorted(responses_raw):
            resolved = _resolve_local_ref(document, responses_raw[code],
                                          gaps, group_scope)
            if not isinstance(resolved, dict):
                continue
            response = _read_response(resolved, code, document, gaps, group_scope)
            if response is None:
                continue
            responses.append(response)
            examples.extend(_extract_examples(
                resolved.get("content"), document, gaps, group_scope,
                response.status_code))
            if response.status_code.isdigit() and int(response.status_code) >= 400:
                error_codes.append(ApiErrorCode(
                    code=response.status_code,
                    description=response.description,
                    http_status=response.status_code))
    else:
        gaps.append(ApiKnowledgeGap(
            gap_type="missing_responses",
            description=f"endpoint {eid} declares no responses mapping",
            endpoint_id=eid))

    # Evidence 覆盖：endpoint 身份（method/path/version_scope）与每个存在事实
    # 都各自绑定；无事实的字段不要求绑定。同一 Evidence 可支撑多个 field_path。
    bindings = [
        ApiFieldBinding(field_path="method", evidence_ids=ids),
        ApiFieldBinding(field_path="path", evidence_ids=ids),
        ApiFieldBinding(field_path="version_scope", evidence_ids=ids),
    ]
    if summary:
        bindings.append(ApiFieldBinding(field_path="summary", evidence_ids=ids))
    if description:
        bindings.append(ApiFieldBinding(field_path="description", evidence_ids=ids))
    for group_name, location in (("path_parameters", "path"),
                                 ("query_parameters", "query"),
                                 ("headers", "header")):
        for param in groups[location]:
            bindings.append(ApiFieldBinding(
                field_path=f"{group_name}.{param.name}", evidence_ids=ids))
    if request_body is not None:
        bindings.append(ApiFieldBinding(field_path="request_body", evidence_ids=ids))
    for response in responses:
        bindings.append(ApiFieldBinding(
            field_path=f"responses.{response.status_code}", evidence_ids=ids))
    for error in error_codes:
        bindings.append(ApiFieldBinding(
            field_path=f"error_codes.{error.code}", evidence_ids=ids))
    for example in examples:
        bindings.append(ApiFieldBinding(
            field_path=f"examples.{example.title}", evidence_ids=ids))

    return ApiEndpoint(
        method=method,
        path=path,
        version_scope=version_scope,
        summary=summary,
        description=description,
        path_parameters=tuple(groups["path"]),
        query_parameters=tuple(groups["query"]),
        headers=tuple(groups["header"]),
        request_body=request_body,
        responses=tuple(responses),
        error_codes=tuple(error_codes),
        examples=tuple(examples),
        evidence_bindings=tuple(bindings),
    )


# ---------------------------------------------------------------------------
# 公共解析入口
# ---------------------------------------------------------------------------


def parse_openapi(
    text: str,
    evidence: Iterable[Mapping[str, Any]] | None = None,
    *,
    format: str | None = None,
    version_scope: str | None = None,
    max_bytes: int = MAX_OPENAPI_BYTES,
    source: str = "openapi",
) -> ApiDocumentIR:
    """OpenAPI 3 JSON/YAML → ApiDocumentIR（纯函数，只读输入）。"""
    document = _load_document(text, format, max_bytes, source)
    gaps: list[ApiKnowledgeGap] = []

    openapi_field = document.get("openapi")
    if not isinstance(openapi_field, str) or not openapi_field.strip():
        raise ApiReferenceParseError(
            f"{source}: missing 'openapi' version field")
    version = openapi_field.strip()
    if not version.startswith("3."):
        raise ApiReferenceParseError(
            f"{source}: unsupported OpenAPI version {version!r} "
            f"(only 3.x is supported)")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ApiReferenceParseError(
            f"{source}: 'paths' must be a mapping (found "
            f"{type(paths).__name__ if paths is not None else 'missing'})")

    version_scope = normalize_version_scope(version_scope)
    raw_refs = tuple(build_evidence_ref(r) for r in (evidence or ()))
    active = tuple(r for r in raw_refs if r.status == "active")

    doc_bindings: list[ApiFieldBinding] = []

    # overview（根级说明事实，需 overview/all 证据；无证据不能成为事实字段）。
    info = document.get("info")
    overview_text = ""
    if isinstance(info, dict):
        overview_text = str(info.get("description", "")).strip()
    if overview_text:
        overview_ids = _matching_ids(active, _matches_overview)
        if overview_ids:
            doc_bindings.append(ApiFieldBinding(
                field_path="overview", evidence_ids=overview_ids))
        else:
            gaps.append(ApiKnowledgeGap(
                gap_type="no_evidence",
                description="document overview described but no matching active "
                            "evidence; not recorded as fact"))
            overview_text = ""

    # authentication（security scheme 事实，需 authentication 证据）。
    auth_list: list[ApiAuthentication] = []
    for auth in _read_security_schemes(document):
        ids = _matching_ids(active, lambda r, n=auth.name:
                            _matches_named(r, "authentication", n))
        if ids:
            auth_list.append(auth)
            doc_bindings.append(ApiFieldBinding(
                field_path=f"authentication.{auth.name}", evidence_ids=ids))
        else:
            gaps.append(ApiKnowledgeGap(
                gap_type="no_evidence",
                description=f"security scheme {auth.name} has no matching active "
                            f"evidence; not recorded as fact"))

    # endpoints（operation 级事实，需 operation 证据）。
    # OpenAPI 允许参数在 path 级与 operation 级：先解析 path 级参数组，
    # operation 级同身份 (location, name) 参数在 _build_endpoint 内覆盖。
    endpoints: list[ApiEndpoint] = []
    for path in sorted(paths):
        path_item = paths[path]
        if not isinstance(path_item, dict):
            continue
        path_level_groups = _parse_parameter_groups(
            path_item.get("parameters"), document, gaps, f"path {path}")
        for method in sorted(path_item):
            if method.lower() not in _METHOD_TOKENS:
                continue
            operation = path_item[method]
            if not isinstance(operation, dict):
                continue
            try:
                normalized_path = normalize_api_path(path)
                normalized_method = normalize_http_method(method)
            except ValueError as exc:
                gaps.append(ApiKnowledgeGap(
                    gap_type="ambiguous_input",
                    description=f"endpoint candidate {method} {path} skipped: {exc}"))
                continue
            endpoint = _build_endpoint(
                operation, normalized_method, normalized_path, document, active,
                gaps, version_scope, path_level_groups)
            if endpoint is not None:
                endpoints.append(endpoint)

    models, model_bindings = _read_data_models(document, active, gaps)

    return ApiDocumentIR(
        overview=overview_text,
        authentication=tuple(auth_list),
        endpoints=tuple(endpoints),
        data_models=tuple(models),
        knowledge_gaps=tuple(gaps),
        evidence_bindings=tuple(doc_bindings + model_bindings),
    )


# ---------------------------------------------------------------------------
# Markdown 确定性 hint 解析
# ---------------------------------------------------------------------------

_MD_ENDPOINT_RE = re.compile(
    r"(?m)^\s*`?\s*(?P<method>[A-Za-z]+)\s+(?P<path>/[^\s`?\"'<>]*)")
_MD_STATUS_RE = re.compile(
    r"(?:HTTP\s+)?(?P<code>[1-5][0-9]{2})(?:[^0-9]|$)", re.IGNORECASE)


def markdown_endpoint_hints(text: str) -> tuple[tuple[str, str], ...]:
    """确定性扫描 Markdown：返回规范化 (METHOD, path) hint 集合（去重排序）。

    Markdown LLM 候选 method/path 必须能与此 hint 集合或原文核对。
    """
    hints: set[tuple[str, str]] = set()
    for match in _MD_ENDPOINT_RE.finditer(text):
        method_token = match.group("method")
        if method_token.lower() not in _METHOD_TOKENS:
            continue
        try:
            method = normalize_http_method(method_token)
            path = normalize_api_path(match.group("path"))
        except ValueError:
            continue
        hints.add((method, path))
    return tuple(sorted(hints))


def markdown_status_code_hints(text: str) -> tuple[str, ...]:
    """确定性扫描 Markdown：返回候选状态/错误码集合（去重排序）。"""
    codes = {m.group("code") for m in _MD_STATUS_RE.finditer(text)}
    return tuple(sorted(codes))


# 原文明示业务错误码：只接受明确“错误码/error code”提示行上的受限 token。
_MD_ERROR_TOKEN_RE = re.compile(r"(?<![A-Z0-9_])([A-Z][A-Z0-9_]{2,})(?![A-Z0-9_])")
_MD_HTTP_INLINE_RE = re.compile(r"HTTP\s+([1-5][0-9]{2})", re.IGNORECASE)
# 已知非业务码的常见大写词，避免把 HTTP/API 等当作错误码。
_MD_ERROR_TOKEN_BLACKLIST = {"HTTP", "HTTPS", "URL", "API"}


def markdown_error_code_hints(text: str) -> tuple[tuple[str, str], ...]:
    """确定性扫描 Markdown 中「原文明示的业务错误码」。

    - 只在包含标记（`错误码` / `error code`，大小写不敏感）的行内提取
      `USER_NOT_FOUND` 这类受限 token；绝不从自然语言猜测错误码；
    - 返回 (code, http_status) 有序对；仅当同一行明确给出 `HTTP nnn` 时才填
      http_status，否则为空字符串（不得按错误码名称推测 HTTP 状态）；
    - 该扫描只服务 error_codes 的核对；HTTP status_codes 仍走
      markdown_status_code_hints。
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        lowered = line.lower()
        if "错误码" not in line and "error code" not in lowered:
            continue
        inline_http = _MD_HTTP_INLINE_RE.search(line)
        status = inline_http.group(1) if inline_http else ""
        for match in _MD_ERROR_TOKEN_RE.finditer(line):
            code = match.group(1)
            if code in _MD_ERROR_TOKEN_BLACKLIST:
                continue
            found.setdefault(code, status)
    return tuple(sorted((code, http) for code, http in found.items()))


def parse_markdown_hints(
    text: str,
    evidence: Iterable[Mapping[str, Any]] | None = None,
    *,
    max_bytes: int = MAX_MARKDOWN_BYTES,
    source: str = "markdown",
) -> ApiDocumentIR:
    """Markdown 文档 → 确定性 hint 集合（不猜参数类型/required/Schema）。

    只产出 hint 型 knowledge_gaps；出现 method+path 才给 endpoint hint，
    状态码/错误码候选给 status hint。任何自然语言描述不进入事实字段。
    evidence 参数仅供调用方保持接口一致；hint 本身不是事实，无需绑定。
    """
    if not isinstance(text, str):
        raise ApiReferenceParseError(f"{source}: input must be str text")
    if len(text.encode("utf-8")) > max_bytes:
        raise ApiReferenceParseError(
            f"{source}: input exceeds size limit {max_bytes} bytes")
    gaps: list[ApiKnowledgeGap] = []

    for method, path in markdown_endpoint_hints(text):
        gaps.append(ApiKnowledgeGap(
            gap_type="markdown_endpoint_hint",
            description=f"candidate endpoint hint: {method} {path} "
                        f"(version scope not declared; parameter types/required/"
                        f"Schema not inferred from natural language)"))

    for code in markdown_status_code_hints(text):
        gaps.append(ApiKnowledgeGap(
            gap_type="markdown_status_hint",
            description=f"candidate status/error code hint: HTTP {code} "
                        f"(semantics not inferred from natural language)"))

    return ApiDocumentIR(knowledge_gaps=tuple(gaps))
