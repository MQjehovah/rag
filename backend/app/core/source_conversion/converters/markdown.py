"""Markdown 转换器：透传 + 移除输入 Frontmatter（单次解析）。

- 收集所有候选证据，返回最高 EvidenceStrength；
- 使用 parse_frontmatter_safe 单次解析，不使用 body.split("---", 2)；
- bytes 严格解码；解码失败 → decode_failed。
"""
from __future__ import annotations

from app.core.source_conversion.base import (
    ConverterMatch,
    EvidenceStrength,
    SourceConverter,
)
from app.core.source_conversion.converters.helpers import pick_strongest
from app.core.source_conversion.frontmatter import parse_frontmatter_safe
from app.core.source_conversion.quality import decode_text_strict, validated_text
from app.core.source_conversion.schemas import (
    ContentKind,
    ConversionDiagnostic,
    ConversionStatus,
    ConverterOutput,
    DiagnosticSeverity,
    RawSourceItem,
    SourceBytesPayload,
    SourceTextPayload,
)

_EXTENSIONS = {"md", "markdown", "mdown", "mkd", "mdx"}
_MIMES = {"text/markdown", "text/x-markdown"}


class MarkdownConverter:
    key = "markdown"
    version = "v1"
    priority = 10
    output_kind = ContentKind.MARKDOWN

    def match(self, item: RawSourceItem) -> ConverterMatch | None:
        candidates: list[ConverterMatch] = []
        if item.content_kind == ContentKind.MARKDOWN:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.CONTENT_KIND,
                priority=self.priority, reason="明确 content_kind=markdown",
                evidence={"content_kind": "markdown"},
            ))
        mime = (item.mime_type or "").lower()
        if mime in _MIMES:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.MIME,
                priority=self.priority, reason=f"精确 MIME {mime}",
                evidence={"mime": mime},
            ))
        if item.extension.lower() in _EXTENSIONS:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.EXTENSION,
                priority=self.priority, reason=f"扩展名 .{item.extension.lower()}",
                evidence={"extension": item.extension.lower()},
            ))
        return pick_strongest(candidates, converter_key=self.key, priority=self.priority)

    def convert(self, item: RawSourceItem, selected_match: ConverterMatch) -> ConverterOutput:
        if isinstance(item.payload, SourceTextPayload):
            text = item.payload.text
        else:
            text, ok = decode_text_strict(item.payload.bytes)
            if not ok:
                return ConverterOutput(
                    body="", status_signal=ConversionStatus.FAILED,
                    diagnostics=(ConversionDiagnostic(
                        code="decode_failed", severity=DiagnosticSeverity.ERROR,
                        message="Markdown 内容无法解码",
                    ),),
                    content_kind=ContentKind.MARKDOWN,
                )
        body = validated_text(text)
        diagnostics: list[ConversionDiagnostic] = []

        # 单次解析输入 Frontmatter
        parsed = parse_frontmatter_safe(body)
        input_frontmatter = dict(parsed.metadata) if parsed.matched else {}
        source_metadata_extra: dict = {}
        if input_frontmatter:
            body = parsed.body  # 直接使用去除后的 body
            source_metadata_extra["input_frontmatter"] = input_frontmatter

        if not (body or "").strip():
            diagnostics.append(ConversionDiagnostic(
                code="empty_body", severity=DiagnosticSeverity.ERROR,
                message="Markdown 正文为空或包含乱码，已拒绝转换",
            ))
        return ConverterOutput(
            body=body or "",
            status_signal=ConversionStatus.FAILED if not (body or "").strip() else ConversionStatus.CONVERTED,
            diagnostics=tuple(diagnostics),
            conversion_metadata={},  # content_kind 不再在 conversion_metadata
            content_kind=ContentKind.MARKDOWN,
            source_metadata_extra=source_metadata_extra,
        )
