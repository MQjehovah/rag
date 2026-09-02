"""纯文本转换器：转换为合法 Markdown 文本。"""
from __future__ import annotations

from app.core.source_conversion.base import (
    ConverterMatch,
    EvidenceStrength,
    SourceConverter,
)
from app.core.source_conversion.converters.helpers import pick_strongest
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

_EXTENSIONS = {
    "txt", "text", "log", "rst", "adoc", "asciidoc",
    "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml", "html",
}


class TextConverter:
    key = "text"
    version = "v1"
    priority = 1
    output_kind = ContentKind.TEXT

    def match(self, item: RawSourceItem) -> ConverterMatch | None:
        candidates: list[ConverterMatch] = []
        # 明确 content_kind=text → CONTENT_KIND(4)（统一矩阵；bytes 载荷也允许进入）
        if item.content_kind == ContentKind.TEXT:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.CONTENT_KIND,
                priority=self.priority, reason="明确 content_kind=text",
                evidence={"content_kind": "text"},
            ))
        mime = (item.mime_type or "").lower()
        if mime.startswith("text/"):
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.MIME,
                priority=self.priority, reason=f"text/* MIME {mime}",
                evidence={"mime": mime},
            ))
        if item.extension.lower() in _EXTENSIONS:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.EXTENSION,
                priority=self.priority, reason=f"文本扩展名 .{item.extension.lower()}",
                evidence={"extension": item.extension.lower()},
            ))
        # 最低证据：纯 SourceTextPayload 且无更强 text 证据 → TEXT_PAYLOAD(1)。
        # SourceBytesPayload 不得仅凭 payload 类型进入文本兜底。
        if isinstance(item.payload, SourceTextPayload):
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.TEXT_PAYLOAD,
                priority=self.priority, reason="SourceTextPayload 文本载荷兜底",
                evidence={"payload_kind": "text"},
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
                        message="文本内容无法解码",
                    ),),
                    content_kind=ContentKind.TEXT,
                )
        body = validated_text(text)
        diagnostics: list[ConversionDiagnostic] = []
        if not (body or "").strip():
            diagnostics.append(ConversionDiagnostic(
                code="empty_body", severity=DiagnosticSeverity.ERROR,
                message="纯文本内容为空或包含乱码，已拒绝转换",
            ))
        return ConverterOutput(
            body=body or "",
            status_signal=ConversionStatus.FAILED if not (body or "").strip() else ConversionStatus.CONVERTED,
            diagnostics=tuple(diagnostics),
            conversion_metadata={},
            content_kind=ContentKind.TEXT,
        )
