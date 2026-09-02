"""PDF 转换器：复用 PDFHybridConverter。"""
from __future__ import annotations

import logging

from app.core.pdf_hybrid_converter import PDFHybridConverter
from app.core.source_conversion.base import (
    ConverterMatch,
    EvidenceStrength,
    SourceConverter,
)
from app.core.source_conversion.converters.helpers import pick_strongest
from app.core.source_conversion.quality import validated_text
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

logger = logging.getLogger(__name__)


class PdfConverter:
    key = "pdf"
    version = "v1"
    priority = 5
    output_kind = ContentKind.PDF

    def __init__(self, hybrid: PDFHybridConverter | None = None):
        self._hybrid = hybrid

    def _converter(self) -> PDFHybridConverter:
        if self._hybrid is None:
            self._hybrid = PDFHybridConverter()
        return self._hybrid

    def match(self, item: RawSourceItem) -> ConverterMatch | None:
        candidates: list[ConverterMatch] = []
        # %PDF 文件头（强证据）
        if item.raw_bytes[:4] == b"%PDF":
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.FILE_HEADER,
                priority=self.priority, reason="%PDF 文件头",
                evidence={"header": "%PDF"},
            ))
        if item.content_kind == ContentKind.PDF:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.CONTENT_KIND,
                priority=self.priority, reason="明确 content_kind=pdf",
                evidence={"content_kind": "pdf"},
            ))
        mime = (item.mime_type or "").lower()
        if mime in {"application/pdf", "application/x-pdf"}:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.MIME,
                priority=self.priority, reason=f"PDF MIME {mime}",
                evidence={"mime": mime},
            ))
        if item.extension.lower() == "pdf":
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.EXTENSION,
                priority=self.priority, reason="扩展名 .pdf",
                evidence={"extension": "pdf"},
            ))
        return pick_strongest(candidates, converter_key=self.key, priority=self.priority)

    def convert(self, item: RawSourceItem, selected_match: ConverterMatch) -> ConverterOutput:
        if isinstance(item.payload, SourceTextPayload):
            return ConverterOutput(
                body="", status_signal=ConversionStatus.FAILED,
                diagnostics=(ConversionDiagnostic(
                    code="legacy_text_payload", severity=DiagnosticSeverity.ERROR,
                    message="PDF 转换要求 bytes 载荷；text 载荷需由 Phase 2 adapter 显式转换",
                ),),
                content_kind=ContentKind.PDF,
            )
        raw_bytes = item.payload.bytes
        conversion_metadata: dict = {}
        diagnostics: list[ConversionDiagnostic] = []
        try:
            result = self._converter().convert(raw_bytes)
        except Exception:  # noqa: BLE001
            logger.error("pdf convert unexpected:\n%s", __import__("traceback").format_exc())
            return ConverterOutput(
                body="", status_signal=ConversionStatus.FAILED,
                diagnostics=(ConversionDiagnostic(
                    code="conversion_failed", severity=DiagnosticSeverity.ERROR,
                    message="PDF 转换失败",
                ),),
                conversion_metadata=conversion_metadata,
                content_kind=ContentKind.PDF,
            )
        body = validated_text(result.text) if result.text else ""
        conversion_metadata["converter"] = result.converter
        conversion_metadata["fallback_used"] = bool(result.fallback_used)
        conversion_metadata.update({k: v for k, v in result.metadata.items()})
        for warning in result.warnings:
            diagnostics.append(ConversionDiagnostic(
                code="converter_warning", severity=DiagnosticSeverity.WARNING,
                message="PDF 转换出现警告",  # 不暴露原始异常/路径
            ))
        has_body = bool((body or "").strip())
        if not has_body:
            diagnostics.insert(0, ConversionDiagnostic(
                code="empty_body", severity=DiagnosticSeverity.ERROR,
                message="PDF 未提取到有效正文",
            ))
        # fallback_used 或警告且有正文 → partial（建议信号）
        if has_body and (result.fallback_used or result.warnings):
            status = ConversionStatus.PARTIAL
        elif has_body:
            status = ConversionStatus.CONVERTED
        else:
            status = ConversionStatus.FAILED
        return ConverterOutput(
            body=body or "",
            status_signal=status,
            diagnostics=tuple(diagnostics),
            conversion_metadata=conversion_metadata,
            content_kind=ContentKind.PDF,
        )
