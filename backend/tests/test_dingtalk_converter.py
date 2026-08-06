import hashlib

import pytest

from app.config import settings
from app.core.dingtalk import DingTalkClient, PDFMarkdownResult
from app.core.dingtalk_converter import (
    CONVERSION_PIPELINE_VERSION,
    DingTalkMarkdownConverter,
)
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.markitdown_adapter import MarkItDownAdapter, PDFPageConversionResult


def make_document(extension="txt", node_id="node-001"):
    return {
        "id": node_id,
        "title": f"测试文档.{extension}",
        "extension": extension,
        "node_type": "FILE",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": f"测试资料/测试文档.{extension}",
        "source_url": f"https://example.test/{node_id}",
        "updated_at": "2026-08-04T10:00:00Z",
    }


def prepare_raw(storage, document, content):
    storage.record_inventory([document])
    storage.persist_raw_file(document, content)
    return storage.read_manifest()["documents"][0]


def test_txt_is_saved_as_utf8_markdown_with_source_metadata(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("txt")
    entry = prepare_raw(storage, document, "本地正文".encode("utf-8"))

    result = DingTalkMarkdownConverter(storage).convert_entry(entry)

    markdown_path = storage.root / result["markdown_path"]
    markdown = markdown_path.read_text(encoding="utf-8")
    assert 'source_type: "dingtalk"' in markdown
    assert 'dingtalk_node_id: "node-001"' in markdown
    assert 'converter: "builtin_text"' in markdown
    assert f'conversion_pipeline_version: "{CONVERSION_PIPELINE_VERSION}"' in markdown
    assert "# 测试文档.txt" in markdown
    assert "本地正文" in markdown
    manifest_entry = storage.read_manifest()["documents"][0]
    assert manifest_entry["status"] == "converted"
    assert manifest_entry["converter"] == "builtin_text"
    assert manifest_entry["conversion_pipeline_version"] == CONVERSION_PIPELINE_VERSION
    assert manifest_entry["markdown_hash"] == hashlib.sha256(
        markdown_path.read_bytes()
    ).hexdigest()


def test_csv_is_converted_to_markdown_table(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("csv")
    entry = prepare_raw(
        storage,
        document,
        "名称,数量\n机器人,2\n".encode("utf-8"),
    )

    result = DingTalkMarkdownConverter(storage).convert_entry(entry)
    markdown = (storage.root / result["markdown_path"]).read_text(encoding="utf-8")

    assert "| 名称 | 数量 |" in markdown
    assert "| --- | --- |" in markdown
    assert "| 机器人 | 2 |" in markdown


def test_markdown_source_content_is_preserved(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("md")
    entry = prepare_raw(storage, document, b"## Existing heading\n\nBody")

    result = DingTalkMarkdownConverter(storage).convert_entry(entry)
    markdown = (storage.root / result["markdown_path"]).read_text(encoding="utf-8")

    assert "## Existing heading" in markdown
    assert "Body" in markdown


def test_mislabeled_office_file_falls_back_to_plain_text(monkeypatch):
    monkeypatch.setattr(
        DingTalkClient,
        "_extract_text",
        staticmethod(lambda _content, _extension: ""),
    )
    license_text = b"Copyright Example. All rights reserved.\nPermission is granted."

    converted = DingTalkMarkdownConverter.convert_content(license_text, "docx")

    assert converted.startswith("Copyright Example")


def test_binary_office_fallback_is_rejected(monkeypatch):
    binary = (b"PK\x03\x04\x00\x01\x02\x03" * 200) + "中文".encode("utf-8")
    monkeypatch.setattr(
        DingTalkClient,
        "_extract_text",
        staticmethod(lambda _content, _extension: binary.decode("utf-8", errors="ignore")),
    )

    assert DingTalkMarkdownConverter.convert_content(binary, "docx") == ""


def test_esafenet_encrypted_source_is_reported_before_format_conversion():
    content = b"\x62\x14\x23\x65\x00\x00E-SafeNet\x00LOCK\x00encrypted"

    result = DingTalkMarkdownConverter().convert_content_result(content, "docx")

    assert result.text == ""
    assert result.converter == "encrypted_source"
    assert result.metadata["source_encryption"] == "E-SafeNet"
    assert "有权限的安全终端" in result.warnings[0]


def test_encrypted_source_reason_and_metadata_are_saved_to_manifest(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("pdf")
    entry = prepare_raw(
        storage,
        document,
        b"\x62\x14\x23\x65\x00\x00E-SafeNet\x00LOCK\x00encrypted",
    )

    with pytest.raises(ValueError, match="E-SafeNet加密文件容器"):
        DingTalkMarkdownConverter(storage).convert_entry(entry)

    failed = storage.read_manifest()["documents"][0]
    assert failed["conversion_status"] == "failed"
    assert failed["converter"] == "encrypted_source"
    assert failed["source_encryption"] == "E-SafeNet"


def test_real_docx_zip_signature_uses_structured_converter(monkeypatch):
    monkeypatch.setattr(settings, "markitdown_enabled", False)
    monkeypatch.setattr(
        DingTalkClient,
        "_docx_to_markdown",
        staticmethod(lambda _content: "结构化正文"),
    )

    converted = DingTalkMarkdownConverter.convert_content(b"PK\x03\x04content", "docx")

    assert converted == "结构化正文"


def test_office_file_prefers_markitdown(tmp_path):
    class FakeMarkItDown:
        SUPPORTED_EXTENSIONS = MarkItDownAdapter.SUPPORTED_EXTENSIONS

        def convert_bytes(self, _content, extension):
            assert extension == "docx"
            return "# MarkItDown结构化正文"

    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("docx")
    entry = prepare_raw(storage, document, b"PK\x03\x04content")
    result = DingTalkMarkdownConverter(
        storage,
        markitdown=FakeMarkItDown(),
    ).convert_entry(entry)

    assert result["converter"] == "markitdown"
    assert result["converter_fallback_used"] is False
    markdown = (storage.root / result["markdown_path"]).read_text("utf-8")
    assert "MarkItDown结构化正文" in markdown


def test_office_markitdown_pads_rows_shortened_by_merged_cells(tmp_path):
    class FakeMarkItDown:
        SUPPORTED_EXTENSIONS = MarkItDownAdapter.SUPPORTED_EXTENSIONS

        def convert_bytes(self, _content, _extension):
            return (
                "| 分类 | 培训内容 | 签字 |\n"
                "| --- | --- | --- |\n"
                "| APP | 正常登录 | 是 |\n"
                "| 客户签字 | |"
            )

    converter = DingTalkMarkdownConverter(
        DingTalkLocalStorage(tmp_path / "dingtalk"),
        markitdown=FakeMarkItDown(),
    )

    result = converter.convert_content_result(b"PK\x03\x04content", "docx")

    assert "| 客户签字 |  |  |" in result.text


def test_office_file_falls_back_when_markitdown_fails(monkeypatch, tmp_path):
    class FailedMarkItDown:
        SUPPORTED_EXTENSIONS = MarkItDownAdapter.SUPPORTED_EXTENSIONS

        def convert_bytes(self, _content, _extension):
            raise RuntimeError("模拟MarkItDown失败")

    monkeypatch.setattr(
        DingTalkClient,
        "_extract_text",
        staticmethod(lambda _content, _extension: "原解析器正文"),
    )
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("pptx")
    entry = prepare_raw(storage, document, b"PK\x03\x04content")
    result = DingTalkMarkdownConverter(
        storage,
        markitdown=FailedMarkItDown(),
    ).convert_entry(entry)

    assert result["converter"] == "legacy_pptx"
    assert result["converter_fallback_used"] is True
    assert "模拟MarkItDown失败" in result["conversion_warnings"][0]


def test_pdf_combines_markitdown_body_with_enhanced_parser(monkeypatch):
    class FakeMarkItDown:
        SUPPORTED_EXTENSIONS = MarkItDownAdapter.SUPPORTED_EXTENSIONS

        def convert_pdf_pages(self, _content):
            return PDFPageConversionResult(
                total_pages=1,
                pages={1: "MarkItDown正文"},
            )

        def convert_bytes(self, _content, _extension):
            return "整份兜底正文"

    monkeypatch.setattr(
        DingTalkClient,
        "_pdf_to_markdown_result",
        staticmethod(lambda _content, page_text_overrides=None: PDFMarkdownResult(
            f"{page_text_overrides[1]}\n\nOCR和视觉增强正文",
            {"pdf_total_pages": 1, "pdf_markitdown_pages": 1},
        )),
    )
    result = DingTalkMarkdownConverter(
        markitdown=FakeMarkItDown()
    ).convert_content_result(b"%PDF-test", "pdf")

    assert result.converter == "hybrid_pdf"
    assert "MarkItDown正文" in result.text
    assert "OCR和视觉增强正文" in result.text


def test_pdf_conversion_statistics_are_saved_to_manifest(monkeypatch, tmp_path):
    class FakeMarkItDown:
        SUPPORTED_EXTENSIONS = MarkItDownAdapter.SUPPORTED_EXTENSIONS

        def convert_pdf_pages(self, _content):
            return PDFPageConversionResult(
                total_pages=1,
                pages={1: "MarkItDown正文"},
            )

        def convert_bytes(self, _content, _extension):
            return "整份兜底正文"

    monkeypatch.setattr(
        DingTalkClient,
        "_pdf_to_markdown_result",
        staticmethod(lambda _content, page_text_overrides=None: PDFMarkdownResult(
            page_text_overrides[1],
            {
                "pdf_total_pages": 1,
                "pdf_total_images": 2,
                "pdf_ocr_scanned_pages": 1,
                "pdf_ocr_added_lines": 3,
                "pdf_visual_candidate_pages": 1,
                "pdf_visual_analyzed_pages": 1,
                "pdf_markitdown_pages": 1,
            },
        )),
    )
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("pdf")
    entry = prepare_raw(storage, document, b"%PDF-test")

    result = DingTalkMarkdownConverter(
        storage,
        markitdown=FakeMarkItDown(),
    ).convert_entry(entry)

    assert result["converter"] == "hybrid_pdf"
    assert result["pdf_total_pages"] == 1
    assert result["pdf_total_images"] == 2
    assert result["pdf_ocr_added_lines"] == 3
    manifest_entry = storage.read_manifest()["documents"][0]
    assert manifest_entry["pdf_visual_analyzed_pages"] == 1
    markdown = (storage.root / result["markdown_path"]).read_text("utf-8")
    assert 'pdf_markitdown_pages: "1"' in markdown


def test_non_pdf_bytes_are_not_sent_to_pdf_parser(monkeypatch):
    monkeypatch.setattr(
        DingTalkClient,
        "_extract_text",
        staticmethod(lambda _content, _extension: pytest.fail("不应解析伪PDF")),
    )

    assert DingTalkMarkdownConverter.convert_content(b"\x00\x01not-a-pdf", "pdf") == ""


def test_pdf_batch_conversion_disables_optional_enhancements(monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_markdown_pdf_enhanced", False)
    monkeypatch.setattr(settings, "pdf_ocr_enabled", True)
    monkeypatch.setattr(settings, "pdf_image_assets_enabled", True)
    monkeypatch.setattr(settings, "pdf_vision_enabled", True)

    def fake_extract(_content, page_text_overrides=None):
        assert settings.pdf_ocr_enabled is False
        assert settings.pdf_image_assets_enabled is False
        assert settings.pdf_vision_enabled is False
        return PDFMarkdownResult(
            "PDF正文",
            {"pdf_total_pages": 1, "pdf_markitdown_pages": 0},
        )

    monkeypatch.setattr(
        settings,
        "markitdown_enabled",
        False,
    )
    monkeypatch.setattr(
        DingTalkClient,
        "_pdf_to_markdown_result",
        staticmethod(fake_extract),
    )

    assert DingTalkMarkdownConverter.convert_content(b"%PDF-test", "pdf") == "PDF正文"
    assert settings.pdf_ocr_enabled is True
    assert settings.pdf_image_assets_enabled is True
    assert settings.pdf_vision_enabled is True


def test_pdf_batch_conversion_preserves_ocr_when_enhanced(monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_markdown_pdf_enhanced", True)
    monkeypatch.setattr(settings, "pdf_ocr_enabled", True)
    monkeypatch.setattr(settings, "pdf_image_assets_enabled", True)
    monkeypatch.setattr(settings, "pdf_vision_enabled", True)

    def fake_extract(_content, page_text_overrides=None):
        assert settings.pdf_ocr_enabled is True
        assert settings.pdf_image_assets_enabled is True
        assert settings.pdf_vision_enabled is True
        return PDFMarkdownResult(
            "增强PDF正文",
            {"pdf_total_pages": 1, "pdf_markitdown_pages": 0},
        )

    monkeypatch.setattr(settings, "markitdown_enabled", False)
    monkeypatch.setattr(
        DingTalkClient,
        "_pdf_to_markdown_result",
        staticmethod(fake_extract),
    )

    assert DingTalkMarkdownConverter.convert_content(b"%PDF-test", "pdf") == "增强PDF正文"


def test_missing_raw_file_is_recorded_as_conversion_failure(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("txt")
    storage.record_inventory([document])
    entry = storage.read_manifest()["documents"][0]

    with pytest.raises(FileNotFoundError):
        DingTalkMarkdownConverter(storage).convert_entry(entry)

    failed = storage.read_manifest()["documents"][0]
    assert failed["status"] == "conversion_failed"
    assert "不存在" in failed["error"]


def test_identical_markdown_is_not_rewritten(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("txt")
    first = storage.persist_markdown_file(document, "# 标题\n")
    second = storage.persist_markdown_file(document, "# 标题\r\n")

    assert first["markdown_write_result"] == "written"
    assert second["markdown_write_result"] == "unchanged"


def test_invalid_generated_markdown_is_quarantined(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = make_document("docx")
    entry = prepare_raw(storage, document, b"plain source")
    metadata = storage.persist_markdown_file(document, "# 标题\n\x00\x01binary")

    result = DingTalkMarkdownConverter(storage).quarantine_invalid_markdown()

    assert result["rejected"] == 1
    assert not (storage.root / metadata["markdown_path"]).exists()
    manifest_entry = storage.read_manifest()["documents"][0]
    assert manifest_entry["status"] == "downloaded"
    assert (storage.root / manifest_entry["rejected_markdown_path"]).is_file()
    assert (storage.root / entry["raw_path"]).is_file()
