from app.config import settings
from app.core.dingtalk import DingTalkClient, PDFMarkdownResult
from app.core.markitdown_adapter import PDFPageConversionResult
from app.core.pdf_hybrid_converter import PDFHybridConverter


class FakeMarkItDown:
    def __init__(self, page_result, whole_text="整份PDF兜底正文"):
        self.page_result = page_result
        self.whole_text = whole_text

    def convert_pdf_pages(self, _content):
        if isinstance(self.page_result, Exception):
            raise self.page_result
        return self.page_result

    def convert_bytes(self, _content, extension):
        assert extension == "pdf"
        return self.whole_text


def test_hybrid_pdf_uses_markitdown_pages_and_keeps_enhancement_metadata(monkeypatch):
    monkeypatch.setattr(settings, "pdf_hybrid_enabled", True)
    monkeypatch.setattr(settings, "markitdown_enabled", True)
    received = {}

    def fake_enhanced(_content, page_text_overrides=None):
        received.update(page_text_overrides or {})
        return PDFMarkdownResult(
            "## 第 1 页\n\nMarkItDown正文\n\n### 本页重要图片",
            {
                "pdf_total_pages": 2,
                "pdf_total_images": 1,
                "pdf_ocr_scanned_pages": 1,
                "pdf_ocr_added_lines": 2,
                "pdf_visual_candidate_pages": 1,
                "pdf_visual_analyzed_pages": 1,
                "pdf_markitdown_pages": 2,
            },
        )

    monkeypatch.setattr(
        DingTalkClient,
        "_pdf_to_markdown_result",
        staticmethod(fake_enhanced),
    )
    markitdown = FakeMarkItDown(PDFPageConversionResult(
        total_pages=2,
        pages={1: "MarkItDown第一页", 2: "MarkItDown第二页"},
    ))

    result = PDFHybridConverter(markitdown).convert(b"%PDF-test")

    assert received == {1: "MarkItDown第一页", 2: "MarkItDown第二页"}
    assert result.converter == "hybrid_pdf"
    assert result.fallback_used is False
    assert result.metadata["pdf_total_images"] == 1
    assert result.metadata["pdf_enhanced_fallback_pages"] == 0


def test_hybrid_pdf_falls_back_only_for_failed_markitdown_page(monkeypatch):
    monkeypatch.setattr(settings, "pdf_hybrid_enabled", True)
    monkeypatch.setattr(settings, "markitdown_enabled", True)

    def fake_enhanced(_content, page_text_overrides=None):
        assert page_text_overrides == {1: "第一页正文"}
        return PDFMarkdownResult(
            "## 第 1 页\n\n第一页正文\n\n## 第 2 页\n\n原文本层正文",
            {"pdf_total_pages": 2, "pdf_markitdown_pages": 1},
        )

    monkeypatch.setattr(
        DingTalkClient,
        "_pdf_to_markdown_result",
        staticmethod(fake_enhanced),
    )
    markitdown = FakeMarkItDown(PDFPageConversionResult(
        total_pages=2,
        pages={1: "第一页正文"},
        failed_pages=[2],
        warnings=["PDF第2页MarkItDown转换失败"],
    ))

    result = PDFHybridConverter(markitdown).convert(b"%PDF-test")

    assert result.converter == "hybrid_pdf"
    assert result.fallback_used is True
    assert result.metadata["pdf_enhanced_fallback_pages"] == 1
    assert result.metadata["pdf_markitdown_failed_pages"] == [2]
    assert "第2页" in result.warnings[0]


def test_hybrid_pdf_uses_whole_markitdown_only_when_enhancement_fails(monkeypatch):
    monkeypatch.setattr(settings, "pdf_hybrid_enabled", True)
    monkeypatch.setattr(settings, "markitdown_enabled", True)
    monkeypatch.setattr(settings, "markitdown_pdf_fallback_enabled", True)
    monkeypatch.setattr(
        DingTalkClient,
        "_pdf_to_markdown_result",
        staticmethod(lambda _content, page_text_overrides=None: None),
    )
    markitdown = FakeMarkItDown(RuntimeError("逐页转换失败"))

    result = PDFHybridConverter(markitdown).convert(b"%PDF-test")

    assert result.converter == "markitdown"
    assert result.fallback_used is True
    assert result.text == "整份PDF兜底正文"
    assert any("逐页PDF转换失败" in warning for warning in result.warnings)
