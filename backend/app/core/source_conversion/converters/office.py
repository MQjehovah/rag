"""Office 转换器：复用 MarkItDownAdapter。"""
from __future__ import annotations

import logging

from app.core.markitdown_adapter import (
    MarkItDownAdapter,
    MarkItDownConversionError,
    MarkItDownUnavailableError,
)
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

_EXTENSIONS = {"docx", "pptx", "xlsx"}
_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class OfficeConverter:
    key = "office"
    version = "v1"
    priority = 5
    output_kind = ContentKind.OFFICE

    _MIME_TO_FORMAT = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    }

    def __init__(self, markitdown: MarkItDownAdapter | None = None):
        self._markitdown = markitdown

    def _adapter(self) -> MarkItDownAdapter:
        if self._markitdown is None:
            self._markitdown = MarkItDownAdapter()
        return self._markitdown

    def match(self, item: RawSourceItem) -> ConverterMatch | None:
        candidates: list[ConverterMatch] = []
        if item.content_kind == ContentKind.OFFICE:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.CONTENT_KIND,
                priority=self.priority, reason="明确 content_kind=office",
                evidence={"content_kind": "office"},
            ))
        mime = (item.mime_type or "").lower()
        if mime in _MIMES:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.MIME,
                priority=self.priority, reason=f"Office MIME {mime}",
                evidence={"mime": mime, "resolved_format": self._MIME_TO_FORMAT.get(mime, "")},
            ))
        if item.extension.lower() in _EXTENSIONS:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.EXTENSION,
                priority=self.priority, reason=f"Office 扩展名 .{item.extension.lower()}",
                evidence={"extension": item.extension.lower()},
            ))
        # 解析最终 format：MIME 优先；否则扩展名；否则未知
        merged = pick_strongest(candidates, converter_key=self.key, priority=self.priority)
        if merged is None:
            return None
        merged_evidence = dict(merged.evidence or {})
        mime_format = self._MIME_TO_FORMAT.get((item.mime_type or "").lower(), "")
        ext_format = item.extension.lower() if item.extension.lower() in _EXTENSIONS else ""
        resolved = mime_format or ext_format or ""
        merged_evidence["resolved_format"] = resolved
        merged_evidence["format_evidence"] = "mime" if mime_format else ("extension" if ext_format else "unknown")
        if mime_format and ext_format and mime_format != ext_format:
            merged_evidence["format_conflict"] = {"mime": mime_format, "extension": ext_format}
        return ConverterMatch(
            converter_key=self.key,
            specificity=merged.specificity,
            priority=self.priority,
            reason=merged.reason,
            evidence=merged_evidence,
        )

    def convert(self, item: RawSourceItem, selected_match: ConverterMatch) -> ConverterOutput:
        if isinstance(item.payload, SourceTextPayload):
            return ConverterOutput(
                body="", status_signal=ConversionStatus.FAILED,
                diagnostics=(ConversionDiagnostic(
                    code="legacy_text_payload", severity=DiagnosticSeverity.ERROR,
                    message="Office 转换要求 bytes 载荷；text 载荷需由 Phase 2 adapter 显式转换",
                ),),
                content_kind=ContentKind.OFFICE,
            )
        raw_bytes = item.payload.bytes
        from collections.abc import Mapping
        evidence = selected_match.evidence or {}
        resolved_format = evidence.get("resolved_format", "") if isinstance(evidence, Mapping) else ""
        diagnostics: list[ConversionDiagnostic] = []
        body = ""
        if not resolved_format:
            diagnostics.append(ConversionDiagnostic(
                code="missing_office_format", severity=DiagnosticSeverity.ERROR,
                message="无法确定 Office 具体格式（docx/pptx/xlsx）",
            ))
        else:
            # MIME 与 extension 冲突 → 记录冲突诊断（MIME 优先，resolved_format 已体现）
            if isinstance(evidence, Mapping) and evidence.get("format_conflict"):
                diagnostics.append(ConversionDiagnostic(
                    code="office_format_conflict", severity=DiagnosticSeverity.WARNING,
                    message="Office MIME 与扩展名不一致，已按 MIME 处理",
                ))
            try:
                text = self._adapter().convert_bytes(raw_bytes, resolved_format)
                body = validated_text(text)
            except MarkItDownUnavailableError:
                diagnostics.append(ConversionDiagnostic(
                    code="converter_unavailable", severity=DiagnosticSeverity.ERROR,
                    message="Office 转换依赖的 MarkItDown 不可用",
                ))
            except MarkItDownConversionError:
                diagnostics.append(ConversionDiagnostic(
                    code="conversion_failed", severity=DiagnosticSeverity.ERROR,
                    message="Office 文档转换失败",
                ))
            except Exception:  # noqa: BLE001
                logger.error("office convert unexpected:\n%s", __import__("traceback").format_exc())
                diagnostics.append(ConversionDiagnostic(
                    code="conversion_failed", severity=DiagnosticSeverity.ERROR,
                    message="Office 文档转换异常",
                ))
        if not (body or "").strip():
            diagnostics.append(ConversionDiagnostic(
                code="empty_body", severity=DiagnosticSeverity.ERROR,
                message="Office 转换后正文为空",
            ))
        return ConverterOutput(
            body=body or "",
            status_signal=ConversionStatus.CONVERTED if (body or "").strip() else ConversionStatus.FAILED,
            diagnostics=tuple(diagnostics),
            conversion_metadata={},
            content_kind=ContentKind.OFFICE,
        )
