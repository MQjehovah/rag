"""CSV/TSV → Markdown Table 转换器（Phase 1.3）。

- 单次解码：raw bytes → decode_text_strict 一次 → parsed rows → table；
- selected_match 决定 resolved_format/delimiter（MIME > extension > default）；
- evidence 记录 resolved_format/delimiter/format_evidence；
- output_kind = CSV。
"""
from __future__ import annotations

import csv
import io

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

_CSV_EXTENSIONS = {"csv"}
_TSV_EXTENSIONS = {"tsv", "tab"}
_CSV_MIMES = {"text/csv"}
_TSV_MIMES = {"text/tab-separated-values", "text/tsv"}


def _markdown_cell(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    lines = [
        "| " + " | ".join(_markdown_cell(cell) for cell in normalized[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_markdown_cell(cell) for cell in row) + " |"
        for row in normalized[1:]
    )
    return "\n".join(lines)


def _parse_rows(decoded_text: str, delimiter: str) -> list[list[str]]:
    """从已解码文本解析行（供 Converter 内部使用，不重复解码）。"""
    text = decoded_text
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    return [
        row for row in csv.reader(io.StringIO(text), delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]


def csv_to_markdown(content: bytes, delimiter: str = ",") -> str:
    """兼容外部调用：bytes → 内部单次解码 → 表格（保留）。"""
    text, _ = decode_text_strict(content)
    return _rows_to_markdown(_parse_rows(text, delimiter))


class CsvTableConverter:
    key = "csv_table"
    version = "v1"
    priority = 5
    output_kind = ContentKind.CSV

    def match(self, item: RawSourceItem) -> ConverterMatch | None:
        candidates: list[ConverterMatch] = []
        if item.content_kind == ContentKind.CSV:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.CONTENT_KIND,
                priority=self.priority, reason="明确 content_kind=csv",
                evidence={"content_kind": "csv"},
            ))
        mime = (item.mime_type or "").lower()
        if mime in _CSV_MIMES | _TSV_MIMES:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.MIME,
                priority=self.priority, reason=f"表格 MIME {mime}",
                evidence={"mime": mime},
            ))
        ext = item.extension.lower()
        if ext in _CSV_EXTENSIONS | _TSV_EXTENSIONS:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.EXTENSION,
                priority=self.priority, reason=f"表格扩展名 .{ext}",
                evidence={"extension": ext},
            ))
        # 解析格式证据：MIME > extension > default
        resolved_format, delimiter, format_evidence = self._resolve_format(item, candidates)
        merged = pick_strongest(candidates, converter_key=self.key, priority=self.priority)
        if merged is None:
            return None
        # 合并 resolved_format 到最强 match 的 evidence
        merged_evidence = dict(merged.evidence or {})
        merged_evidence.update({
            "resolved_format": resolved_format,
            "delimiter": delimiter,
            "format_evidence": format_evidence,
        })
        return ConverterMatch(
            converter_key=self.key,
            specificity=merged.specificity,
            priority=self.priority,
            reason=merged.reason,
            evidence=merged_evidence,
        )

    def _resolve_format(self, item: RawSourceItem, candidates: list[ConverterMatch]) -> tuple[str, str, str]:
        """按 MIME > extension > default 解析确定性格式。"""
        mime = (item.mime_type or "").lower()
        if mime in _TSV_MIMES:
            return "tsv", "\t", "mime"
        if mime in _CSV_MIMES:
            return "csv", ",", "mime"
        ext = item.extension.lower()
        if ext in _TSV_EXTENSIONS:
            return "tsv", "\t", "extension"
        if ext in _CSV_EXTENSIONS:
            return "csv", ",", "extension"
        return "csv", ",", "default"

    def convert(self, item: RawSourceItem, selected_match: ConverterMatch) -> ConverterOutput:
        # 用 selected_match 的确定性格式
        evidence = selected_match.evidence or {}
        delimiter = evidence.get("delimiter", ",")
        resolved_format = evidence.get("resolved_format", "csv")

        if isinstance(item.payload, SourceTextPayload):
            raw_bytes = item.payload.text.encode("utf-8")
        else:
            raw_bytes = item.payload.bytes

        # 单次解码
        text, decode_ok = decode_text_strict(raw_bytes)
        diagnostics: list[ConversionDiagnostic] = []
        if not decode_ok:
            diagnostics.append(ConversionDiagnostic(
                code="decode_failed", severity=DiagnosticSeverity.ERROR,
                message="表格内容无法解码",
            ))
        body = ""
        if decode_ok:
            rows = _parse_rows(text, delimiter)
            body = validated_text(_rows_to_markdown(rows))
        if not (body or "").strip():
            diagnostics.append(ConversionDiagnostic(
                code="empty_body", severity=DiagnosticSeverity.ERROR,
                message="表格内容为空或未提取到有效行",
            ))
        status = ConversionStatus.FAILED if (not (body or "").strip() or not decode_ok) else ConversionStatus.CONVERTED
        format_evidence = evidence.get("format_evidence", "default") if isinstance(evidence, dict) or hasattr(evidence, "get") else "default"
        return ConverterOutput(
            body=body or "",
            status_signal=status,
            diagnostics=tuple(diagnostics),
            conversion_metadata={
                "delimiter": delimiter,
                "resolved_format": resolved_format,
                "format_evidence": format_evidence,
            },
            content_kind=ContentKind.CSV,
        )
