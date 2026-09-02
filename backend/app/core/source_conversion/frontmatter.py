"""Phase 1.3：Frontmatter 边界（有界扫描 + 深层冻结 + Renderer）。

- parse_frontmatter_safe：有界扫描（限定 Header 字节/行数内找闭合，超过立即 unmatched），
  不先对整份文档 DOTALL 搜索再判断大小；
- ParsedFrontmatter.metadata 深层冻结；
- Renderer 对 input_frontmatter 先 thaw_value 再 json.dumps；
- input_frontmatter 存命名空间字段，不平铺用户 key。
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.source_conversion.schemas import CanonicalNote, freeze_value, thaw_value

_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_LINES = 200

_FRONTMATTER_OPEN = "---\n"


def _finite(value: float) -> bool:
    return math.isfinite(value)


@dataclass(frozen=True)
class ParsedFrontmatter:
    metadata: Mapping  # 深层冻结的 mapping
    body: str
    matched: bool

    def __post_init__(self) -> None:
        # metadata 必须 Mapping（list/tuple/str → ValueError）
        if not isinstance(self.metadata, Mapping):
            raise ValueError(f"ParsedFrontmatter.metadata 必须是 Mapping，收到 {type(self.metadata).__name__}")
        if not isinstance(self.body, str):
            raise ValueError(f"ParsedFrontmatter.body 必须是 str，收到 {type(self.body).__name__}")
        if not isinstance(self.matched, bool):
            raise ValueError(f"ParsedFrontmatter.matched 必须是 bool，收到 {type(self.matched).__name__}")
        object.__setattr__(self, "metadata", freeze_value(self.metadata))


def _parse_yaml_like(raw: str) -> dict:
    """安全解析受限标量/标量列表；禁止对象构造。"""
    result: dict = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = value
        if isinstance(parsed, (str, int, float, bool)) or parsed is None:
            if isinstance(parsed, float) and not _finite(parsed):
                continue
            result[key] = parsed
        elif isinstance(parsed, list) and all(
            isinstance(v, (str, int, float, bool)) or v is None for v in parsed
        ):
            if not all(_finite(v) for v in parsed if isinstance(v, float)):
                continue
            result[key] = list(parsed)
    return result


def parse_frontmatter_safe(markdown: str) -> ParsedFrontmatter:
    """精确 delimiter + 有上界 find 的 Frontmatter 解析（不扫描全文）。

    实现方式（代码层证据，非"测试正确即算法有界"）：
    - 输入必须 str（非 str → ValueError）；
    - 开头只接受精确 "---\\n" / "---\\r\\n"（"---oops\\n"、"---   \\n"、"----\\n"、"---\\t\\n" → unmatched）；
    - 闭合行只接受精确 "---" / "---\\r"（"  ---"、"---  " 等带空白 → unmatched，不用 strip() 放宽）；
    - 所有 Header 换行 find 都传 end 上界（opener_end + _MAX_HEADER_BYTES + 1 个字符）；
    - 增量累计 UTF-8 字节数，超 64KB 立即 unmatched（2MB 无换行单行在边界内退出）；
    - 绝不调用原始全文的 split/splitlines/encode/无 end 的 find；
    - 找到闭合位置后才允许切出 body。
    """
    if not isinstance(markdown, str):
        raise ValueError("parse_frontmatter_safe 输入必须是 str")

    def _fail() -> ParsedFrontmatter:
        return ParsedFrontmatter(metadata={}, body=markdown, matched=False)

    # ---- 开头精确匹配：仅 "---\n" 或 "---\r\n" ----
    opener_end = None
    if markdown.startswith("---\n"):
        opener_end = 4
    elif markdown.startswith("---\r\n"):
        opener_end = 5
    if opener_end is None:
        return _fail()

    # ---- 固定字符上界：UTF-8 每字符至少 1 字节，最多需看 opener_end + _MAX_HEADER_BYTES + 1 字符 ----
    char_bound = opener_end + _MAX_HEADER_BYTES + 1
    bounded = markdown[:char_bound]

    pos = opener_end
    line_count = 1  # opener 行
    # 增量累计已扫描 Header 的 UTF-8 字节数
    scanned_bytes = len(markdown[:opener_end].encode("utf-8"))

    while True:
        if line_count > _MAX_HEADER_LINES + 1:
            return _fail()
        # 增量累计：当前行内容（不含换行）
        nl = bounded.find("\n", pos, char_bound)  # 所有 Header find 都有 end 上界
        if nl == -1:
            # 受限前缀内找不到换行：若文档实际超过边界 → 立即 unmatched（2MB 单行提前退出）
            if len(markdown) >= char_bound:
                return _fail()
            # 文档确实在边界内结束，最后一行可能是闭合（无尾随换行）
            line = markdown[pos:]
            if line == "---" or line == "---\r":
                header_raw = markdown[opener_end:pos]
                scanned_bytes += len(line.encode("utf-8"))
                if scanned_bytes > _MAX_HEADER_BYTES:
                    return _fail()
                return _finish(header_raw, "")
            return _fail()

        line = markdown[pos:nl]
        # 闭合行精确匹配："---" / "---\r"
        if line == "---" or line == "---\r":
            header_raw = markdown[opener_end:pos]
            body = markdown[nl + 1:]
            scanned_bytes += len(line.encode("utf-8"))
            if scanned_bytes > _MAX_HEADER_BYTES:
                return _fail()
            return _finish(header_raw, body)
        # 普通 Header 行：累计字节
        scanned_bytes += len(line.encode("utf-8")) + 1  # +1 换行
        if scanned_bytes > _MAX_HEADER_BYTES:
            return _fail()
        pos = nl + 1
        line_count += 1

    # 不可达
    return _fail()


def _finish(header_raw: str, body: str) -> ParsedFrontmatter:
    """从受限 Header 前缀解析 metadata，并返回 ParsedFrontmatter。

    此处才允许切出 body（body 的正常返回/strip 不属于 Header 搜索）。
    """
    header_lines = header_raw.split("\n")
    if len(header_lines) > _MAX_HEADER_LINES:
        return ParsedFrontmatter(metadata={}, body=body, matched=False)
    metadata = _parse_yaml_like("\n".join(header_lines))
    if not metadata:
        return ParsedFrontmatter(metadata={}, body=body, matched=False)
    return ParsedFrontmatter(metadata=metadata, body=body.strip(), matched=True)


def extract_input_frontmatter(markdown: str) -> dict:
    """兼容入口：返回 metadata（mapping）或空 dict。"""
    result = parse_frontmatter_safe(markdown)
    return dict(result.metadata)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

_SYSTEM_KEYS = {
    "title", "schema_version", "source_type", "source_id", "source_url",
    "source_path", "source_hash", "body_hash", "converter", "converter_key",
    "converter_version", "conversion_status", "content_kind", "acl_scope",
    "input_frontmatter", "external_version",
}


def _frontmatter_value(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def render_frontmatter(note: CanonicalNote) -> str:
    metadata: dict[str, str] = {
        "title": _single_line(note.title or ""),
        "schema_version": note.schema_version,
        "source_type": note.source_type,
        "source_id": note.source_id,
        "source_url": note.identity.source_url if note.identity else "",
        "source_path": note.identity.source_path if note.identity else "",
        "external_version": note.identity.external_version if note.identity else "",
        "source_hash": note.source_hash or "",
        "body_hash": note.body_hash or "",
        "converter": note.converter_key,
        "converter_version": note.converter_version,
        "conversion_status": note.conversion_status.value,
        "content_kind": note.content_kind.value if note.content_kind else "",
    }
    if not note.acl.resolve_failed:
        metadata["acl_scope"] = json.dumps(
            {"scope": note.acl.scope, "groups": sorted(note.acl.groups)},
            ensure_ascii=False,
        )

    # input_frontmatter 命名空间字段：先 thaw（解嵌套 MappingProxy）再 json.dumps，
    # 写入时用 _frontmatter_value 包成 JSON 字符串（可被 _parse_yaml_like 恢复）。
    input_fm = (note.source_metadata or {}).get("input_frontmatter")
    if isinstance(input_fm, Mapping):
        input_fm_json = json.dumps(thaw_value(dict(input_fm)), ensure_ascii=False)
        metadata["input_frontmatter"] = input_fm_json

    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {_frontmatter_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def _single_line(value: str) -> str:
    return " ".join((value or "").replace("\r", " ").replace("\n", " ").split())


def render_markdown_file(note: CanonicalNote, *, with_title: bool = True) -> str:
    frontmatter = render_frontmatter(note)
    title = _single_line(note.title or "")
    parts = [frontmatter]
    if with_title and title:
        parts.append(f"# {title}")
    body = (note.body or "").strip()
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def parse_frontmatter(markdown: str) -> tuple[dict, str]:
    result = parse_frontmatter_safe(markdown)
    return dict(result.metadata), result.body
