"""Phase 1.3：内置 Converter 行为 + CanonicalNoteService 状态归一 + 异常边界 +
CSV/TSV delimiter + Frontmatter 端到端 + 新旧钉钉等价。"""
from __future__ import annotations

import pytest

from app.core.markitdown_adapter import MarkItDownUnavailableError
from app.core.source_conversion.converters import build_builtin_registry
from app.core.source_conversion.converters.office import OfficeConverter
from app.core.source_conversion.converters.pdf import PdfConverter
from app.core.source_conversion.frontmatter import (
    parse_frontmatter,
    parse_frontmatter_safe,
    render_frontmatter,
    render_markdown_file,
)
from app.core.source_conversion.schemas import (
    CanonicalNote,
    ContentKind,
    ConversionDiagnostic,
    ConversionStatus,
    ConverterOutput,
    DiagnosticSeverity,
    RawSourceItem,
    SourceACLView,
    SourceIdentity,
    source_bytes_payload,
    source_text_payload,
)
from app.core.source_conversion.service import CanonicalNoteService

_SID = lambda st="test", sid="x": SourceIdentity(source_type=st, source_id=sid)


def _item(text: str = "hello", ext: str = "txt", **over):
    base = dict(
        source_type="test", external_id="x",
        payload=source_text_payload(text),
        filename=f"a.{ext}", extension=ext, title="T",
    )
    base.update(over)
    return RawSourceItem(**base)


def _svc():
    return CanonicalNoteService()


def _svc_unsealed():
    return CanonicalNoteService(build_builtin_registry(seal=False))


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_same_item_converts_identically():
    item = _item("# md", ext="md")
    n1 = _svc().convert(item)
    n2 = _svc().convert(item)
    assert n1.converter_key == n2.converter_key == "markdown"
    assert n1.body_hash == n2.body_hash
    assert n1.source_hash == n2.source_hash


# ---------------------------------------------------------------------------
# Markdown / Frontmatter
# ---------------------------------------------------------------------------


def test_markdown_passthrough():
    note = _svc().convert(_item("# 标题\n\n正文", ext="md"))
    assert note.converter_key == "markdown"
    assert note.body == "# 标题\n\n正文"


def test_markdown_removes_input_frontmatter():
    md = "---\ntitle: 旧标题\ncustom: 值\n---\n\n# 正文\n\n内容"
    note = _svc().convert(_item(md, ext="md"))
    assert not note.body.startswith("---")
    assert note.body == "# 正文\n\n内容"
    assert note.source_metadata.get("input_frontmatter") == {"title": "旧标题", "custom": "值"}
    assert note.title == "T"


def test_markdown_middle_separator_preserved():
    note = _svc().convert(_item("# 标题\n\n---\n\n中间", ext="md"))
    assert "---" in note.body
    assert "input_frontmatter" not in note.source_metadata


def test_markdown_header_value_containing_fence_not_truncated():
    md = "---\ntitle: 包含---分隔\n---\n\n正文"
    note = _svc().convert(_item(md, ext="md"))
    assert note.source_metadata.get("input_frontmatter") == {"title": "包含---分隔"}
    assert note.body == "正文"


def test_markdown_unclosed_frontmatter_not_removed():
    md = "---\ntitle: x\n# 没有闭合\n\n正文"
    note = _svc().convert(_item(md, ext="md"))
    assert "input_frontmatter" not in note.source_metadata
    assert "title: x" in note.body


def test_markdown_empty_mapping_not_removed():
    note = _svc().convert(_item("---\n# 只有注释\n---\n\n正文", ext="md"))
    assert "input_frontmatter" not in note.source_metadata


def test_markdown_strict_decode_fails():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"\xff\xff\xff\xffgarbage"), extension="md",
    )
    note = _svc().convert(item)
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "decode_failed" for d in note.diagnostics)
    assert "�" not in note.body


def test_nested_input_frontmatter_rendered_and_recovered():
    """嵌套 MappingProxy input_frontmatter 可渲染并恢复（Renderer 先 thaw 再 json.dumps）。"""
    note = CanonicalNote(
        body="正文",
        identity=_SID(),
        source_hash="a" * 64,
        converter_key="markdown", converter_version="v1",
        conversion_status=ConversionStatus.CONVERTED, content_kind=ContentKind.MARKDOWN,
        source_metadata={"input_frontmatter": {"nested": {"a": 1}}},
    )
    rendered = render_frontmatter(note)
    assert "input_frontmatter" in rendered
    # 用 parse_frontmatter 解析，恢复嵌套 dict（值是一次 JSON 字符串 → 一次 json.loads 得 dict）
    import json
    meta, _ = parse_frontmatter(rendered)
    value = json.loads(meta["input_frontmatter"])
    assert value == {"nested": {"a": 1}}


# ---------------------------------------------------------------------------
# 源代码
# ---------------------------------------------------------------------------


def test_source_code_wrapped_in_fence():
    note = _svc().convert(_item("def f():\n    return 1", ext="py", content_kind=ContentKind.CODE))
    assert note.body == "```python\ndef f():\n    return 1\n```"


def test_source_code_existing_fence_uses_longer():
    code = "```\ninner\n```\nrest"
    note = _svc().convert(_item(code, ext="py", content_kind=ContentKind.CODE))
    assert note.body.startswith("````python\n")
    assert note.body.endswith("\n````")


def test_source_code_node_shebang_with_py_outputs_javascript():
    """node shebang + .py → javascript 围栏（selected_match 证据优先）。"""
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("#!/usr/bin/env node\nconsole.log(1)"),
        filename="app.py", extension="py",
    )
    note = _svc().convert(item)
    assert note.body.startswith("```javascript\n")


def test_source_code_python_shebang_with_js_outputs_python():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("#!/usr/bin/env python\nprint(1)"),
        filename="app.js", extension="js",
    )
    note = _svc().convert(item)
    assert note.body.startswith("```python\n")


def test_source_code_dockerfile_special_filename():
    note = _svc().convert(_item("FROM python:3.12\n", ext="", filename="Dockerfile"))
    assert note.converter_key == "source_code"
    assert note.body == "```dockerfile\nFROM python:3.12\n```"


def test_source_code_strict_decode_fails():
    item = _item(ext="py", content_kind=ContentKind.CODE,
                 payload=source_bytes_payload(b"\xff\xff\xff\xffgarbage"))
    note = _svc().convert(item)
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "decode_failed" for d in note.diagnostics)


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------


def test_csv_to_markdown_table():
    note = _svc().convert(_item("name,age\nAlice,30\n", ext="csv"))
    assert note.body.splitlines()[0] == "| name | age |"
    assert "| Alice | 30 |" in note.body


def test_tsv_uses_tab():
    note = _svc().convert(_item("name\tage\nAlice\t30\n", ext="tsv"))
    assert note.body.splitlines()[0] == "| name | age |"


def test_csv_with_tsv_mime_uses_tab():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a\tb\n1\t2\n"),
        filename="a.csv", extension="csv", mime_type="text/tab-separated-values",
    )
    note = _svc().convert(item)
    assert note.conversion_metadata["delimiter"] == "\t"
    assert note.body.splitlines()[0] == "| a | b |"


def test_tsv_with_csv_mime_uses_comma():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a,b\n1,2\n"),
        filename="a.tsv", extension="tsv", mime_type="text/csv",
    )
    note = _svc().convert(item)
    assert note.conversion_metadata["delimiter"] == ","
    assert note.body.splitlines()[0] == "| a | b |"


def test_content_kind_csv_with_tsv_mime_uses_tab():
    """content_kind=csv + TSV MIME + .csv → tab（格式证据 MIME 优先）。"""
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a\tb\n1\t2\n"),
        filename="a.csv", extension="csv", mime_type="text/tab-separated-values",
        content_kind=ContentKind.CSV,
    )
    note = _svc().convert(item)
    assert note.conversion_metadata["delimiter"] == "\t"
    assert note.conversion_metadata["resolved_format"] == "tsv"
    assert note.body.splitlines()[0] == "| a | b |"


def test_content_kind_csv_with_csv_mime_and_tsv_ext_uses_comma():
    """content_kind=csv + CSV MIME + .tsv → comma（MIME 优先于扩展名）。"""
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a,b\n1,2\n"),
        filename="a.tsv", extension="tsv", mime_type="text/csv",
        content_kind=ContentKind.CSV,
    )
    note = _svc().convert(item)
    assert note.conversion_metadata["delimiter"] == ","
    assert note.conversion_metadata["resolved_format"] == "csv"
    assert note.body.splitlines()[0] == "| a | b |"


def test_content_kind_csv_only_tsv_ext():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a\tb\n1\t2\n"),
        filename="a.tsv", extension="tsv", content_kind=ContentKind.CSV,
    )
    note = _svc().convert(item)
    assert note.conversion_metadata["delimiter"] == "\t"
    assert note.conversion_metadata["format_evidence"] == "extension"


def test_content_kind_csv_no_mime_ext_uses_default_comma():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a,b\n1,2\n"),
        filename="f.bin", extension="bin", content_kind=ContentKind.CSV,
    )
    note = _svc().convert(item)
    assert note.conversion_metadata["delimiter"] == ","
    assert note.conversion_metadata["format_evidence"] == "default"


def test_csv_decode_failed_diagnostic():
    item = _item(ext="csv", payload=source_bytes_payload(b"\xff\xff\xff\xffgarbage"))
    note = _svc().convert(item)
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code in ("decode_failed", "empty_body") for d in note.diagnostics)


def test_csv_single_decode_only(monkeypatch):
    """CSV Converter 只解码一次（monkeypatch 计数）。"""
    from app.core.source_conversion.converters import csv_table

    calls = {"n": 0}
    original = csv_table.decode_text_strict

    def counting(content):
        calls["n"] += 1
        return original(content)

    monkeypatch.setattr(csv_table, "decode_text_strict", counting)
    item = _item("name,age\nA,1\n", ext="csv")
    note = _svc().convert(item)
    assert note.body.splitlines()[0] == "| name | age |"
    assert calls["n"] == 1  # 只解码一次


# ---------------------------------------------------------------------------
# 纯文本
# ---------------------------------------------------------------------------


def test_plain_text_content_kind_text():
    note = _svc().convert(_item("plain", ext="txt", content_kind=ContentKind.TEXT))
    assert note.converter_key == "text"
    assert note.body == "plain"


def test_text_payload_with_content_kind_text_enters_text_converter():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("hello"),
        extension="bin", filename="f.bin", content_kind=ContentKind.TEXT,
    )
    note = _svc().convert(item)
    assert note.converter_key == "text"
    assert note.body == "hello"


def test_text_bytes_with_content_kind_text_enters_text_converter():
    """content_kind=text + bytes 载荷允许进入 TextConverter 并严格解码。"""
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"hello bytes"),
        extension="bin", filename="f.bin", content_kind=ContentKind.TEXT,
    )
    note = _svc().convert(item)
    assert note.converter_key == "text"
    assert note.body == "hello bytes"


def test_unknown_bytes_blocked():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"\x00\x01\x02garbage"),
        extension="zzz", filename="f.zzz",
    )
    note = _svc().convert(item)
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "unsupported_format" for d in note.diagnostics)


# ---------------------------------------------------------------------------
# Office / PDF 复用
# ---------------------------------------------------------------------------


def test_office_reuses_markitdown_adapter():
    class FakeMarkItDown:
        def convert_bytes(self, content: bytes, extension: str) -> str:
            assert extension == "docx"
            return "# Word 内容"

    office = OfficeConverter(markitdown=FakeMarkItDown())  # type: ignore[arg-type]
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_bytes_payload(b"PK"), extension="docx", filename="a.docx",
    )
    out = office.convert(item, office.match(item))  # type: ignore[arg-type]
    assert out.body == "# Word 内容"
    assert out.status_signal == ConversionStatus.CONVERTED


def test_office_mime_only_derives_format():
    """MIME-only docx/pptx/xlsx → resolved_format 正确。"""

    class FakeMarkItDown:
        seen: list[str] = []

        def convert_bytes(self, content: bytes, extension: str) -> str:
            FakeMarkItDown.seen.append(extension)
            return f"# {extension}"

    office = OfficeConverter(markitdown=FakeMarkItDown())  # type: ignore[arg-type]
    mime_map = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    }
    for mime, expected_ext in mime_map.items():
        item = RawSourceItem(
            source_type="test", external_id="x",
            payload=source_bytes_payload(b"PK"), extension="", filename="doc",
            mime_type=mime, content_kind=ContentKind.OFFICE,
        )
        out = office.convert(item, office.match(item))  # type: ignore[arg-type]
        assert FakeMarkItDown.seen[-1] == expected_ext, f"{mime} → {expected_ext}"


def test_office_mime_extension_conflict_mime_wins():
    """MIME 与扩展名冲突时，MIME 优先，并记录冲突诊断。"""

    class FakeMarkItDown:
        seen: list[str] = []

        def convert_bytes(self, content: bytes, extension: str) -> str:
            FakeMarkItDown.seen.append(extension)
            return "# x"

    office = OfficeConverter(markitdown=FakeMarkItDown())  # type: ignore[arg-type]
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_bytes_payload(b"PK"), extension="xlsx", filename="a.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content_kind=ContentKind.OFFICE,
    )
    # match 产生 conflict 证据
    from app.core.source_conversion.base import EvidenceStrength
    match = office.match(item)
    assert match is not None
    assert "format_conflict" in match.evidence
    out = office.convert(item, match)
    assert FakeMarkItDown.seen[-1] == "docx"  # MIME 优先
    assert any(d.code == "office_format_conflict" for d in out.diagnostics)


def test_office_unknown_format_fails():
    """content_kind=office 但无法确定具体格式 → missing_office_format。"""
    office = OfficeConverter(markitdown=None)
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_bytes_payload(b"PK"), extension="", filename="doc",
        content_kind=ContentKind.OFFICE,
    )
    out = office.convert(item, office.match(item))  # type: ignore[arg-type]
    assert out.status_signal == ConversionStatus.FAILED
    assert any(d.code == "missing_office_format" for d in out.diagnostics)


def test_office_text_payload_rejected():
    office = OfficeConverter()
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_text_payload("not binary"), extension="docx",
    )
    out = office.convert(item, office.match(item))  # type: ignore[arg-type]
    assert out.status_signal == ConversionStatus.FAILED
    assert any(d.code == "legacy_text_payload" for d in out.diagnostics)


def test_office_markitdown_unavailable():
    class Unavailable:
        def convert_bytes(self, content, extension):
            raise MarkItDownUnavailableError("not installed")

    office = OfficeConverter(markitdown=Unavailable())  # type: ignore[arg-type]
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_bytes_payload(b"PK"), extension="docx",
    )
    out = office.convert(item, office.match(item))  # type: ignore[arg-type]
    assert out.status_signal == ConversionStatus.FAILED
    assert any(d.code == "converter_unavailable" for d in out.diagnostics)


def test_pdf_reuses_hybrid_and_partial_on_fallback():
    class FakeHybrid:
        def convert(self, content):
            from app.core.markitdown_adapter import MarkdownConversionResult
            return MarkdownConversionResult("PDF 正文", "hybrid_pdf", fallback_used=True,
                                            warnings=["w"], metadata={"pdf_total_pages": 5})

    svc = _svc_unsealed()
    svc.register(PdfConverter(hybrid=FakeHybrid()), replace=True)  # type: ignore[arg-type]
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_bytes_payload(b"%PDF fake"), extension="pdf",
    )
    note = svc.convert(item)
    assert note.converter_key == "pdf"
    assert note.body == "PDF 正文"
    assert note.conversion_status == ConversionStatus.PARTIAL


def test_pdf_text_payload_rejected():
    pdf = PdfConverter()
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_text_payload("%PDF text"), extension="pdf",
    )
    out = pdf.convert(item, pdf.match(item))  # type: ignore[arg-type]
    assert out.status_signal == ConversionStatus.FAILED
    assert any(d.code == "legacy_text_payload" for d in out.diagnostics)


# ---------------------------------------------------------------------------
# Service：状态归一 / 异常边界 / 契约违规 / 保留字段 / output_kind
# ---------------------------------------------------------------------------


def test_unsupported_format_blocked():
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_bytes_payload(b"\x00\x01garbage"), extension="zzz",
    )
    note = _svc().convert(item)
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "unsupported_format" for d in note.diagnostics)


def test_encrypted_source_blocked():
    item = RawSourceItem(
        source_type="test", external_id="x",
        payload=source_bytes_payload(b"E-SafeNet LOCK fake"), extension="pdf",
    )
    note = _svc().convert(item)
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "encrypted_source" for d in note.diagnostics)


def _make_converter(key, body="正文", content_kind=ContentKind.TEXT, status_signal=None,
                    diagnostics=(), metadata=None, raise_on_convert=False):
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength

    class _C:
        def __init__(self):
            self.key = key
            self.version = "v1"
            self.priority = 5
            self.output_kind = content_kind

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            if raise_on_convert:
                raise RuntimeError("boom")
            return ConverterOutput(
                body=body, content_kind=content_kind,
                status_signal=status_signal, diagnostics=diagnostics,
                conversion_metadata=metadata or {},
            )

    return _C()


def test_converter_raising_yields_failed_with_key():
    svc = _svc_unsealed()
    svc.register(_make_converter("boom", raise_on_convert=True))
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert note.converter_key == "boom"
    assert any(d.code == "conversion_failed" for d in note.diagnostics)


def test_converter_returning_non_converteroutput_fails_gracefully():
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength

    class _Bad:
        key = "bad"
        version = "v1"
        priority = 5
        output_kind = ContentKind.TEXT

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return {"body": "not converteroutput"}  # type: ignore[return-value]

    svc = _svc_unsealed()
    svc.register(_Bad())
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)


def test_converter_returning_invalid_converteroutput_fails_gracefully():
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength

    class _BadDiag:
        key = "baddiag"
        version = "v1"
        priority = 5
        output_kind = ContentKind.TEXT

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", diagnostics=({"code": "x"},))  # type: ignore[arg-type]

    svc = _svc_unsealed()
    svc.register(_BadDiag())
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)


def test_output_kind_mismatch_fails():
    """Converter 输出 content_kind 与声明 output_kind 不一致 → failed + contract_violation。"""
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength

    class _Lying:
        key = "lying"
        version = "v1"
        priority = 5
        output_kind = ContentKind.MARKDOWN  # 声明 markdown

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.PDF)  # 实际 PDF

    svc = _svc_unsealed()
    svc.register(_Lying())
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)


def test_converter_cannot_override_reserved_fields():
    """Converter 覆盖 selected_match/candidate_matches → contract_violation。"""
    svc = _svc_unsealed()
    svc.register(_make_converter("reserved", metadata={"selected_match": {"fake": 1}}))
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)


def test_selected_match_preserved_in_conversion_metadata():
    """Service 写入的 selected_match/candidate_matches 不被 converter 覆盖。"""
    svc = _svc_unsealed()
    svc.register(_make_converter("ok", metadata={"delimiter": ","}))
    note = svc.convert(_item(ext="x1"))
    assert "selected_match" in note.conversion_metadata
    assert "candidate_matches" in note.conversion_metadata
    assert note.conversion_metadata["selected_match"]["converter_key"] == "ok"


def test_signal_failed_with_body_not_silently_converted():
    svc = _svc_unsealed()
    svc.register(_make_converter("contra", body="有正文", status_signal=ConversionStatus.FAILED))
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert note.body == ""


def test_warning_with_body_partial():
    svc = _svc_unsealed()
    svc.register(_make_converter(
        "warn", diagnostics=(ConversionDiagnostic(code="w", severity=DiagnosticSeverity.WARNING),)))
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.PARTIAL


def test_info_does_not_cause_partial():
    svc = _svc_unsealed()
    svc.register(_make_converter(
        "info", diagnostics=(ConversionDiagnostic(code="i", severity=DiagnosticSeverity.INFO),)))
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.CONVERTED


def test_no_body_no_error_auto_empty_body():
    svc = _svc_unsealed()
    svc.register(_make_converter("empt", body="", diagnostics=()))
    note = svc.convert(_item(ext="x1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "empty_body" for d in note.diagnostics)


def test_suggested_title_only_when_item_title_empty():
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength

    class _Title:
        key = "tt"
        version = "v1"
        priority = 5
        output_kind = ContentKind.TEXT

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", suggested_title="建议标题", content_kind=ContentKind.TEXT)

    svc = _svc_unsealed()
    svc.register(_Title())
    note = svc.convert(_item(ext="x1", title=""))
    assert note.title == "建议标题"
    note2 = svc.convert(_item(ext="x1", title="明确标题"))
    assert note2.title == "明确标题"


# ---------------------------------------------------------------------------
# content_kind 单一权威
# ---------------------------------------------------------------------------


def test_content_kind_single_source_no_duplication():
    note = _svc().convert(_item("# m", ext="md"))
    assert note.content_kind == ContentKind.MARKDOWN
    assert "content_kind" not in note.conversion_metadata


def test_selected_match_in_conversion_metadata():
    item = _item("# m", ext="md")
    note = _svc().convert(item)
    assert note.conversion_metadata["selected_match"]["converter_key"] == "markdown"


# ---------------------------------------------------------------------------
# Frontmatter Renderer
# ---------------------------------------------------------------------------


def test_frontmatter_end_to_end_recovery():
    md = "---\nauthor: alice\ntags: [\"a\", \"b\"]\n---\n\n# 正文"
    item = RawSourceItem(
        source_type="dingtalk", external_id="doc-1",
        payload=source_text_payload(md), filename="a.md", extension="md", title="真实标题",
        acl=SourceACLView(scope="s1", groups=frozenset({"eng"}), resolve_failed=False),
    )
    note = _svc().convert(item)
    rendered = render_markdown_file(note)
    meta, body = parse_frontmatter(rendered)
    assert meta.get("input_frontmatter")
    import json
    # input_frontmatter 值是 JSON 字符串：一次 json.loads 得到 dict
    input_fm = json.loads(meta["input_frontmatter"])
    assert input_fm["author"] == "alice"
    assert input_fm["tags"] == ["a", "b"]
    assert meta["source_id"] == "doc-1"
    assert meta["source_type"] == "dingtalk"
    assert meta["content_kind"] == "markdown"
    assert body == "# 真实标题\n\n# 正文"


def test_renderer_emits_single_frontmatter_and_single_line_title():
    note = CanonicalNote(
        body="---\ninner\n---\n\n正文", title="多\n行\n标题",
        identity=_SID(), source_hash="a" * 64,
        converter_key="markdown", converter_version="v1",
        conversion_status=ConversionStatus.CONVERTED, content_kind=ContentKind.MARKDOWN,
    )
    rendered = render_markdown_file(note)
    assert rendered.startswith("---\n")
    assert "# 多 行 标题" in rendered
    first_closing = rendered.find("\n---\n")
    assert first_closing > 0
    header = rendered[: first_closing + 5]
    assert header.startswith("---\n") and header.endswith("\n---\n")
    assert "---\ninner\n---\n\n正文" in rendered


# ---------------------------------------------------------------------------
# 新旧钉钉等价（兼容移植）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    b"hello world",
    b"\xe4\xb8\xad\xe6\x96\x87\xe5\x86\x85\xe5\xae\xb9",
    b"gbk \xd6\xd0\xce\xc4\xb2\xe2\xca\xd4",
    "中文 ｆ1二".encode("utf-16"),
    b"line1\r\nline2\rline3",
    b"",
])
def test_decode_text_equals_dingtalk_v24(raw):
    from app.core.dingtalk_converter import DingTalkMarkdownConverter
    from app.core.source_conversion import quality
    assert quality.decode_text(raw) == DingTalkMarkdownConverter._decode_text(raw)


@pytest.mark.parametrize("text", [
    "hello", "中文内容", "a\r\nb\rc", "", "\x00invalid",
    "ok\n\f page break", "garbage\x01\x02\x03", "�" * 20,
])
def test_validated_text_equals_dingtalk_v24(text):
    from app.core.dingtalk_converter import DingTalkMarkdownConverter
    from app.core.source_conversion import quality
    assert quality.validated_text(text) == DingTalkMarkdownConverter._validated_text(text)


@pytest.mark.parametrize("content", [b"E-SafeNet LOCK fake", b"normal content", b""])
def test_encrypted_source_type_equals_dingtalk_v24(content):
    from app.core.dingtalk_converter import DingTalkMarkdownConverter
    from app.core.source_conversion import quality
    assert quality.encrypted_source_type(content) == DingTalkMarkdownConverter._encrypted_source_type(content)


@pytest.mark.parametrize("csv_bytes", [
    b"a,b\n1,2\n",
    b"name,note\n\"Alice\",\"line1\nline2\"\n",
    b"a|b,2\n",
    b"a,b\n1\n2,3,4\n",
    b"",
])
def test_csv_markdown_equals_dingtalk_v24(csv_bytes):
    from app.core.dingtalk_converter import DingTalkMarkdownConverter
    from app.core.source_conversion.converters.csv_table import csv_to_markdown
    assert csv_to_markdown(csv_bytes, delimiter=",") == DingTalkMarkdownConverter._csv_to_markdown(csv_bytes)


def test_frontmatter_value_equals_dingtalk_v24():
    from app.core.dingtalk_converter import DingTalkMarkdownConverter
    from app.core.source_conversion.frontmatter import _frontmatter_value
    for value in ["标题", "with\"quote\"", "", "123", None, "a\nb"]:
        assert _frontmatter_value(value) == DingTalkMarkdownConverter._frontmatter_value(value)


def test_decode_strict_rejects_binary():
    """decode_text_strict 对明显二进制返回 ok=False（严格拒绝，名字与报告一致）。"""
    from app.core.source_conversion import quality
    binary = b"\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08" * 20
    text, ok = quality.decode_text_strict(binary)
    assert ok is False
    assert text == ""


def test_decode_strict_accepts_utf16_bom_only():
    from app.core.source_conversion import quality
    # 合法 UTF-16 BOM + 偶数长度
    text, ok = quality.decode_text_strict("中文".encode("utf-16"))
    assert ok is True
    assert text == "中文"
    # 奇数长度 UTF-16 BOM → 拒绝
    text2, ok2 = quality.decode_text_strict(b"\xff\xfe\x00")
    assert ok2 is False
