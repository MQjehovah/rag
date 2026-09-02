"""Phase 1.5：最小封板修正的独立复现测试。

覆盖：
- Frontmatter 真正有界（不 split 全文、1MB 正文仍受限前缀、64KB 单行 Header 提前退出、
  输入非 str、metadata 非 Mapping）；
- Registry 严格规范化（字符串 output_kind 拒绝、key/version 带空白拒绝、注册后篡改 fail closed）；
- ConverterSelection 完整相等关系；
- 对象层小边界（converter_key/version strip、diagnostics=None）。
"""
from __future__ import annotations

import pytest

from app.core.source_conversion.base import ConverterMatch, EvidenceStrength
from app.core.source_conversion.converters import build_builtin_registry
from app.core.source_conversion.registry import SourceConverterRegistry
from app.core.source_conversion.schemas import (
    CanonicalNote,
    ContentKind,
    ConversionDiagnostic,
    ConversionStatus,
    ConverterOutput,
    DiagnosticSeverity,
    RawSourceItem,
    SourceIdentity,
    source_text_payload,
)
from app.core.source_conversion.service import CanonicalNoteService

_SHA = "a" * 64


# ---------------------------------------------------------------------------
# Frontmatter 真正有界
# ---------------------------------------------------------------------------


class _WatchingStr(str):
    """str 子类：监控是否被无界 split（sep=None 即按空白全文切分）。"""

    def split(self, sep=None, maxsplit=-1):
        if sep is None:
            raise AssertionError("对全文调用无分隔符的 split 被拒绝")
        return super().split(sep, maxsplit)

    def splitlines(self, *args, **kwargs):
        raise AssertionError("对全文调用 splitlines 被拒绝")


def test_frontmatter_does_not_split_full_text():
    """用 str 子类监控：parse_frontmatter_safe 不得对全文 split。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = _WatchingStr("---\ntitle: x\n---\n" + "line\n" * 100)
    result = parse_frontmatter_safe(md)
    assert result.matched is True


def test_frontmatter_1mb_body_still_bounded():
    """>1MB 正文，Header 搜索仍只处理受限前缀（matched=True 且不扫描正文）。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\ntitle: x\n---\n" + "x" * (1024 * 1024)
    result = parse_frontmatter_safe(md)
    assert result.matched is True
    assert result.metadata["title"] == "x"


def test_frontmatter_single_line_header_over_64kb_early_exit():
    """单行 Header 超过 64KB → 提前退出 unmatched。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\nkey: " + "x" * (70 * 1024) + "\n---\n正文"
    result = parse_frontmatter_safe(md)
    assert result.matched is False


def test_frontmatter_metadata_must_be_mapping():
    from app.core.source_conversion.frontmatter import ParsedFrontmatter
    for bad in ([], ("a",), "str"):
        with pytest.raises(ValueError):
            ParsedFrontmatter(metadata=bad, body="", matched=False)  # type: ignore[arg-type]


def test_frontmatter_parse_input_must_be_str():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    with pytest.raises(ValueError):
        parse_frontmatter_safe(123)  # type: ignore[arg-type]


def test_frontmatter_crlf_and_lf():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    assert parse_frontmatter_safe("---\r\ntitle: x\r\n---\r\n正文").matched is True
    assert parse_frontmatter_safe("---\ntitle: x\n---\n正文").matched is True


# ---------------------------------------------------------------------------
# Registry 严格规范化（Phase 1.5：拒绝字符串 output_kind / 带空白 key/version）
# ---------------------------------------------------------------------------


def _minimal_converter(key="k", version="v1", output_kind=ContentKind.TEXT):
    class _C:
        def __init__(self):
            self.key = key
            self.version = version
            self.priority = 5
            self.output_kind = output_kind

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=self.output_kind)

    return _C()


def test_register_rejects_string_output_kind():
    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_minimal_converter(output_kind="text"))  # type: ignore[arg-type]


def test_register_rejects_whitespace_key():
    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_minimal_converter(key=" s "))


def test_register_rejects_whitespace_version():
    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_minimal_converter(version=" v1 "))


def test_valid_converter_service_path_no_crash():
    svc = CanonicalNoteService(build_builtin_registry(seal=False))
    svc.register(_minimal_converter(key="ok", output_kind=ContentKind.TEXT))
    item = RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"),
                         filename="f.bin", extension="bin", content_kind=ContentKind.TEXT)
    note = svc.convert(item)
    assert note.conversion_status == ConversionStatus.CONVERTED


def test_mutated_output_kind_after_registration_fail_closed():
    """Converter 注册后篡改 output_kind → Service fail closed（blocked），不得 AttributeError。"""
    conv = _minimal_converter(key="mut", output_kind=ContentKind.TEXT)
    reg = SourceConverterRegistry()
    reg.register(conv)
    # 注册后篡改 output_kind 为字符串
    conv.output_kind = "text"  # type: ignore[assignment]
    item = RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"),
                         filename="f.bin", extension="bin", content_kind=ContentKind.TEXT)
    note = CanonicalNoteService(reg).convert(item)
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "converter_match_failed" for d in note.diagnostics)


def test_mutated_key_after_registration_fail_closed():
    conv = _minimal_converter(key="mut2", output_kind=ContentKind.TEXT)
    reg = SourceConverterRegistry()
    reg.register(conv)
    conv.key = " hacked "  # type: ignore[assignment]
    item = RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"),
                         filename="f.bin", extension="bin", content_kind=ContentKind.TEXT)
    note = CanonicalNoteService(reg).convert(item)
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "converter_match_failed" for d in note.diagnostics)


# ---------------------------------------------------------------------------
# ConverterSelection 完整相等关系
# ---------------------------------------------------------------------------


def test_selection_requires_full_match_not_just_key():
    """selected=a/FILE_HEADER、candidate=a/EXTENSION → ValueError（非完整相等）。"""
    from app.core.source_conversion.base import ConverterSelection
    sel = ConverterMatch("a", EvidenceStrength.FILE_HEADER, priority=5, reason="x")
    cand = ConverterMatch("a", EvidenceStrength.EXTENSION, priority=5, reason="x")
    with pytest.raises(ValueError):
        ConverterSelection(selected_converter=_FakeConverter("a"), selected_match=sel,
                           candidate_matches=(cand,))


def test_selection_identical_match_object_ok():
    from app.core.source_conversion.base import ConverterSelection
    m = ConverterMatch("a", EvidenceStrength.EXTENSION, priority=5, reason="x")
    selection = ConverterSelection(selected_converter=_FakeConverter("a"), selected_match=m,
                                   candidate_matches=(m,))
    assert selection.selected_match == m


class _FakeConverter:
    def __init__(self, key="a"):
        self.key = key
        self.version = "v1"
        self.priority = 5
        self.output_kind = ContentKind.TEXT


# ---------------------------------------------------------------------------
# 对象层小边界
# ---------------------------------------------------------------------------


def test_canonical_note_converter_key_whitespace_rejected():
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity=SourceIdentity(source_type="t", source_id="x"),
                      source_hash=_SHA, converter_key="   ", converter_version="v1",
                      conversion_status=ConversionStatus.FAILED,
                      diagnostics=(ConversionDiagnostic(code="e", severity=DiagnosticSeverity.ERROR),))


def test_canonical_note_converter_version_whitespace_rejected():
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity=SourceIdentity(source_type="t", source_id="x"),
                      source_hash=_SHA, converter_key="k", converter_version="   ",
                      conversion_status=ConversionStatus.FAILED,
                      diagnostics=(ConversionDiagnostic(code="e", severity=DiagnosticSeverity.ERROR),))


def test_converter_output_diagnostics_none_rejected():
    with pytest.raises(ValueError):
        ConverterOutput(body="x", diagnostics=None)  # type: ignore[arg-type]


def test_canonical_note_diagnostics_none_rejected():
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity=SourceIdentity(source_type="t", source_id="x"),
                      source_hash=_SHA, converter_key="k", converter_version="v1",
                      content_kind=ContentKind.TEXT, diagnostics=None)  # type: ignore[arg-type]
