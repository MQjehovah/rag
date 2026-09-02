"""公共 Source → CanonicalNote 转换基础（Phase 1.2 最终封板）。"""
from app.core.source_conversion.base import (
    AmbiguousConverterError,
    ConverterCategory,
    ConverterMatch,
    ConverterMatchError,
    ConverterSelection,
    EvidenceStrength,
    SourceConverter,
)
from app.core.source_conversion.converters import build_builtin_registry
from app.core.source_conversion.frontmatter import (
    ParsedFrontmatter,
    parse_frontmatter_safe,
)
from app.core.source_conversion.registry import SourceConverterRegistry
from app.core.source_conversion.schemas import (
    CanonicalNote,
    ContentKind,
    ConversionDiagnostic,
    ConversionStatus,
    ConverterOutput,
    DiagnosticSeverity,
    RawSourceItem,
    SourceACLView,
    SourceBytesPayload,
    SourceIdentity,
    SourcePayload,
    SourceTextPayload,
    canonical_source_hash,
    source_bytes_payload,
    source_text_payload,
)
from app.core.source_conversion.service import CanonicalNoteService

__all__ = [
    "AmbiguousConverterError",
    "CanonicalNote",
    "CanonicalNoteService",
    "ContentKind",
    "ConversionDiagnostic",
    "ConversionStatus",
    "ConverterCategory",
    "ConverterMatch",
    "ConverterMatchError",
    "ConverterOutput",
    "ConverterSelection",
    "DiagnosticSeverity",
    "EvidenceStrength",
    "ParsedFrontmatter",
    "RawSourceItem",
    "SourceACLView",
    "SourceBytesPayload",
    "SourceConverter",
    "SourceConverterRegistry",
    "SourceIdentity",
    "SourcePayload",
    "SourceTextPayload",
    "build_builtin_registry",
    "canonical_source_hash",
    "parse_frontmatter_safe",
    "source_bytes_payload",
    "source_text_payload",
]
