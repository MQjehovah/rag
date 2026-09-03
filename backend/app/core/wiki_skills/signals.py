"""Phase 6：确定性信号提取（不调用 LLM，只代表「可能适用」）。

检测类型（固定顺序，确定性强）：http_method / api_path / status_code / error_code /
request_json / response_json / parameter_table / step_sequence / config_key_value /
version_note / generic_text。同输入 → 同信号同顺序；扫描字符数、信号数量均有界；
固定正则 + 输入长度有界（避免 ReDoS）。信号值只保留最小摘要，不存完整正文。
"""
from __future__ import annotations

import re

from app.core.wiki_skills.schemas import ApplicabilitySignal

# 扫描上限（超出截断，防超长输入 + 控制正则输入长度）。
_CONTENT_SCAN_LIMIT = 20_000
_TITLE_SCAN_LIMIT = 512
# 信号 value 最小摘要长度（截断防止保存完整正文）。
_VALUE_LIMIT = 96
# 每来源最大信号数（有界）。
_MAX_SIGNALS = 12

# 固定信号类型顺序（router / snapshot 排序稳定依据）。
SIGNAL_TYPE_ORDER = (
    "http_method",
    "api_path",
    "status_code",
    "error_code",
    "request_json",
    "response_json",
    "parameter_table",
    "step_sequence",
    "config_key_value",
    "version_note",
    "generic_text",
)

# 特异性信号类型（generic_text 之外的），供 router 判定「非普通文本」。
SPECIFIC_TYPES = tuple(t for t in SIGNAL_TYPE_ORDER if t != "generic_text")

# 固定、无灾难性回溯的正则（全部 anchored/字符类受限）。
_RE_HTTP_METHOD = re.compile(
    r"(?m)(?<!\w)(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)(?!\w)"
)
_RE_API_PATH = re.compile(
    r"(?i)(?<!\w)/(?:api|v\d+)/[A-Za-z0-9_./{}$:?&=%-]{1,160}"
)
_RE_STATUS_CODE = re.compile(
    r"(?i)(?:http/\S+\s+)?(?:状态码|status\s*(?:code)?|返回)\s*[:：=]?\s*"
    r"(200|201|204|301|304|400|401|403|404|405|409|422|429|500|501|502|503)\b"
)
_RE_ERROR_CODE = re.compile(
    r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"
)
_RE_REQUEST_JSON = re.compile(
    r"(?i)(请求参数|请求体|请求示例|请求json|request\s*(?:body|parameters|payload|json))"
)
_RE_RESPONSE_JSON = re.compile(
    r"(?i)(返回参数|响应体|返回示例|响应示例|响应json|response\s*(?:body|parameters|payload|json))"
)
_RE_PARAMETER_TABLE = re.compile(
    r"(?i)(参数表|参数说明|字段说明|参数列表|field\s+description|parameter\s+table)"
)
_RE_STEP_SEQUENCE = re.compile(
    r"(?i)(操作步骤|使用步骤|实现步骤|步骤\s*[:：]?|step\s*\d|第[一二三四五六七八九十百]+步)"
)
_RE_CONFIG_KV = re.compile(
    r"(?i)(配置项|配置文件|config(?:uration)?\s*[:：]|环境变量|参数说明)\s*"
    r"[:：]?[\s\S]{0,120}?[\n]?\s*[A-Za-z_][A-Za-z0-9_]*\s*[=:]"
)
_RE_VERSION_NOTE = re.compile(
    r"(?i)(版本说明|版本更新|版本升级|升级说明|兼容性|版本兼容|\bversion\b|"
    r"v\d+(?:\.\d+){1,3}\b|版本\s*[:：]?\s*\d+(?:\.\d+)+)"
)

# source=title 时允许的类型（标题中出现多属 API/版本文档标题）。
_TITLE_TYPES = ("http_method", "api_path", "version_note", "generic_text")


def _clip(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= _VALUE_LIMIT else value[:_VALUE_LIMIT] + "…"


def _scan(text: str, limit: int, kind: str) -> str | None:
    """对固定长度输入扫描，返回最小摘要（未命中 None）。kind 决定正则。"""
    if not text:
        return None
    bounded = text[:limit]
    if kind == "http_method":
        m = _RE_HTTP_METHOD.search(bounded)
        return _clip(m.group(1)) if m else None
    if kind == "api_path":
        m = _RE_API_PATH.search(bounded)
        return _clip(m.group(0)) if m else None
    if kind == "status_code":
        m = _RE_STATUS_CODE.search(bounded)
        return _clip(m.group(1)) if m else None
    if kind == "error_code":
        m = _RE_ERROR_CODE.search(bounded)
        return _clip(m.group(0)) if m else None
    if kind == "request_json":
        m = _RE_REQUEST_JSON.search(bounded)
        return _clip(m.group(0)) if m else None
    if kind == "response_json":
        m = _RE_RESPONSE_JSON.search(bounded)
        return _clip(m.group(0)) if m else None
    if kind == "parameter_table":
        m = _RE_PARAMETER_TABLE.search(bounded)
        return _clip(m.group(0)) if m else None
    if kind == "step_sequence":
        m = _RE_STEP_SEQUENCE.search(bounded)
        return _clip(m.group(0)) if m else None
    if kind == "config_key_value":
        m = _RE_CONFIG_KV.search(bounded)
        return "config_key_value" if m else None
    if kind == "version_note":
        m = _RE_VERSION_NOTE.search(bounded)
        return _clip(m.group(0)) if m else None
    return None


def _type_weight(kind: str) -> float:
    """信号类型确定性强度（generic_text 最低，特异性类型高）。"""
    if kind == "generic_text":
        return 0.2
    if kind in ("version_note",):
        return 0.3
    return 1.0


def signal_weight(kind: str) -> float:
    """供 router 计算候选确定性分数的信号强度。"""
    return _type_weight(kind)


def _detect(text: str, limit: int, source: str, allowed: tuple[str, ...]) -> list[ApplicabilitySignal]:
    hits: list[ApplicabilitySignal] = []
    for kind in allowed:
        value = _scan(text, limit, kind)
        if value is not None:
            hits.append(ApplicabilitySignal(
                signal_type=kind,
                value=value,
                strength=_type_weight(kind),
                source=source,
            ))
    return hits


def extract_signals(
    title: str,
    content: str,
    content_kind: str = "",
) -> tuple[ApplicabilitySignal, ...]:
    """从标题/正文/内容类型确定性提取信号（不修改输入、不调 LLM、有界）。

    返回顺序：先 title 来源（title 类型），后 content 来源（deterministic_parser
    类型）；同类型保留 content 来源（去重）；generic_text 恒作兜底（内容或标题非空）。
    """
    title_text = (title or "")[: _TITLE_SCAN_LIMIT]
    content_text = content or ""
    has_any = bool(title_text.strip() or content_text.strip())
    if not has_any:
        return ()

    title_hits = _detect(title_text, _TITLE_SCAN_LIMIT, "title", _TITLE_TYPES)
    content_hits = _detect(
        content_text, _CONTENT_SCAN_LIMIT, "deterministic_parser", SIGNAL_TYPE_ORDER
    )

    # 同类型合并：content（deterministic_parser）优先于 title。
    merged: dict[str, ApplicabilitySignal] = {}
    for sig in list(title_hits) + list(content_hits):
        if sig.signal_type in merged:
            continue
        merged[sig.signal_type] = sig

    # generic_text 兜底：内容/标题任一非空即产出（default Skill fallback 适用）。
    if "generic_text" not in merged:
        source = "title" if title_text.strip() and not content_text.strip() else "deterministic_parser"
        merged["generic_text"] = ApplicabilitySignal(
            signal_type="generic_text",
            value="generic_text",
            strength=_type_weight("generic_text"),
            source=source,
        )

    # 有界 + 稳定排序（按 SIGNAL_TYPE_ORDER）。
    ordered = [merged[t] for t in SIGNAL_TYPE_ORDER if t in merged]
    return tuple(ordered[:_MAX_SIGNALS])
