from types import SimpleNamespace

import pytest

from app.core.markitdown_adapter import (
    MarkItDownAdapter,
    MarkItDownConversionError,
)


def test_adapter_converts_bytes_with_extension_hint():
    class FakeEngine:
        def convert_stream(self, stream, file_extension):
            assert stream.read() == b"office-content"
            assert file_extension == ".docx"
            return SimpleNamespace(text_content="# 标题\n\n正文")

    converted = MarkItDownAdapter(FakeEngine()).convert_bytes(
        b"office-content",
        "docx",
    )

    assert converted == "# 标题\n\n正文"


def test_adapter_rejects_unsupported_extension():
    with pytest.raises(MarkItDownConversionError, match="不处理"):
        MarkItDownAdapter(object()).convert_bytes(b"content", "csv")


def test_adapter_rejects_empty_result():
    class EmptyEngine:
        def convert_stream(self, _stream, file_extension):
            assert file_extension == ".xlsx"
            return SimpleNamespace(text_content="")

    with pytest.raises(MarkItDownConversionError, match="未从xlsx"):
        MarkItDownAdapter(EmptyEngine()).convert_bytes(b"content", "xlsx")


def test_adapter_converts_pdf_page_by_page():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    source = __import__("io").BytesIO()
    writer.write(source)

    class FakeEngine:
        calls = 0

        def convert_stream(self, stream, file_extension):
            assert stream.read(4) == b"%PDF"
            assert file_extension == ".pdf"
            self.calls += 1
            return SimpleNamespace(text_content=f"第{self.calls}页正文")

    result = MarkItDownAdapter(FakeEngine()).convert_pdf_pages(source.getvalue())

    assert result.total_pages == 2
    assert result.pages == {1: "第1页正文", 2: "第2页正文"}
    assert result.failed_pages == []


def test_adapter_records_single_pdf_page_failure_without_stopping_other_pages():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    source = __import__("io").BytesIO()
    writer.write(source)

    class PartiallyFailedEngine:
        calls = 0

        def convert_stream(self, _stream, file_extension):
            assert file_extension == ".pdf"
            self.calls += 1
            return SimpleNamespace(
                text_content="第一页正文" if self.calls == 1 else ""
            )

    result = MarkItDownAdapter(
        PartiallyFailedEngine()
    ).convert_pdf_pages(source.getvalue())

    assert result.pages == {1: "第一页正文"}
    assert result.failed_pages == [2]
    assert "第2页" in result.warnings[0]
