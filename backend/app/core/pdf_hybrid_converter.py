from typing import Any, Dict, List

from app.config import settings
from app.core.dingtalk import DingTalkClient
from app.core.markitdown_adapter import (
    MarkdownConversionResult,
    MarkItDownAdapter,
)


class PDFHybridConverter:
    """融合MarkItDown正文与原有PDF表格、OCR、图片和视觉增强能力。"""

    def __init__(self, markitdown: MarkItDownAdapter | None = None):
        self.markitdown = markitdown or MarkItDownAdapter()

    def convert(self, content: bytes) -> MarkdownConversionResult:
        if not content.startswith(b"%PDF"):
            return MarkdownConversionResult(
                "",
                "plain_text_fallback",
                fallback_used=True,
                warnings=["输入内容不是有效的PDF文件"],
            )

        page_texts: Dict[int, str] = {}
        failed_pages: List[int] = []
        warnings: List[str] = []
        markitdown_attempted = bool(
            settings.pdf_hybrid_enabled and settings.markitdown_enabled
        )

        if markitdown_attempted:
            try:
                page_result = self.markitdown.convert_pdf_pages(content)
                page_texts = dict(page_result.pages)
                failed_pages = list(page_result.failed_pages)
                warnings.extend(page_result.warnings)
            except Exception as exc:
                warnings.append(f"MarkItDown逐页PDF转换失败，已使用增强解析器: {exc}")

        enhanced = DingTalkClient._pdf_to_markdown_result(
            content,
            page_text_overrides=page_texts,
        )
        if enhanced and enhanced.text.strip():
            metadata: Dict[str, Any] = dict(enhanced.metadata)
            total_pages = int(metadata.get("pdf_total_pages") or 0)
            fallback_pages = max(total_pages - len(page_texts), 0)
            metadata.update({
                "pdf_markitdown_failed_pages": failed_pages,
                "pdf_enhanced_fallback_pages": fallback_pages,
            })
            return MarkdownConversionResult(
                enhanced.text,
                "hybrid_pdf" if page_texts else "enhanced_pdf",
                fallback_used=bool(markitdown_attempted and fallback_pages),
                warnings=warnings,
                metadata=metadata,
            )

        if settings.markitdown_enabled and settings.markitdown_pdf_fallback_enabled:
            try:
                text = self.markitdown.convert_bytes(content, "pdf")
                return MarkdownConversionResult(
                    text,
                    "markitdown",
                    fallback_used=True,
                    warnings=warnings + ["增强PDF解析失败，已使用整份MarkItDown结果兜底"],
                    metadata={
                        "pdf_markitdown_failed_pages": failed_pages,
                    },
                )
            except Exception as exc:
                warnings.append(f"MarkItDown整份PDF兜底失败: {exc}")

        return MarkdownConversionResult(
            "",
            "enhanced_pdf",
            fallback_used=True,
            warnings=warnings,
            metadata={
                "pdf_markitdown_failed_pages": failed_pages,
            },
        )
