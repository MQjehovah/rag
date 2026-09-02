"""内置 SourceConverter 集合。"""

from __future__ import annotations

from app.core.source_conversion.converters.csv_table import CsvTableConverter
from app.core.source_conversion.converters.markdown import MarkdownConverter
from app.core.source_conversion.converters.office import OfficeConverter
from app.core.source_conversion.converters.pdf import PdfConverter
from app.core.source_conversion.converters.source_code import SourceCodeConverter
from app.core.source_conversion.converters.text import TextConverter
from app.core.source_conversion.registry import SourceConverterRegistry

__all__ = [
    "CsvTableConverter",
    "MarkdownConverter",
    "OfficeConverter",
    "PdfConverter",
    "SourceCodeConverter",
    "TextConverter",
    "build_builtin_registry",
]


def build_builtin_registry(*, seal: bool = True) -> SourceConverterRegistry:
    """构建内置转换器 Registry 并默认 seal（避免运行中被意外修改）。"""
    registry = SourceConverterRegistry()
    registry.register(MarkdownConverter())
    registry.register(TextConverter())
    registry.register(SourceCodeConverter())
    registry.register(CsvTableConverter())
    registry.register(OfficeConverter())
    registry.register(PdfConverter())
    if seal:
        registry.seal()
    return registry
