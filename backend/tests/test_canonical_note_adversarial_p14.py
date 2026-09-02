"""Phase 1.4：独立对抗测试（封板新增复现用例）。

覆盖：Frontmatter 有界扫描、ACL fail-closed 边界、from_dict 严格化、
output_kind 契约、源代码语言解析、TEXT_PAYLOAD 最低证据、对象层类型不变量、
对象关系强制。
"""
from __future__ import annotations

import json

import pytest

from app.core.source_conversion.base import ConverterMatch, EvidenceStrength
from app.core.source_conversion.converters import build_builtin_registry
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

_SHA = "a" * 64


def _item(ext: str = "bin", **over):
    base = dict(source_type="t", external_id="x", payload=source_text_payload("hi"),
                filename=f"a.{ext}", extension=ext)
    base.update(over)
    return RawSourceItem(**base)


def _svc():
    return CanonicalNoteService(build_builtin_registry(seal=False))


def _valid_cnote(**over):
    base = dict(
        body="x", identity=SourceIdentity(source_type="t", source_id="x"),
        source_hash=_SHA, converter_key="k", converter_version="v1",
        conversion_status=ConversionStatus.CONVERTED, content_kind=ContentKind.TEXT,
    )
    base.update(over)
    return CanonicalNote(**base)


class _FakeConv:
    def __init__(self, key="a"):
        self.key = key
        self.version = "v1"
        self.priority = 5
        self.output_kind = ContentKind.TEXT

    def match(self, item):
        return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

    def convert(self, item, selected_match):
        return ConverterOutput(body="x", content_kind=ContentKind.TEXT)


# ---------------------------------------------------------------------------
# Frontmatter 有界扫描：正文长短不影响匹配
# ---------------------------------------------------------------------------


def test_frontmatter_short_header_long_body_matches():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\ntitle: x\n---\n" + "\n".join(f"line{i}" for i in range(1000))
    result = parse_frontmatter_safe(md)
    assert result.matched is True
    assert result.metadata["title"] == "x"


def test_frontmatter_short_header_huge_body_matches():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\ntitle: x\n---\n" + "x" * (1024 * 1024)  # >1MB 正文
    result = parse_frontmatter_safe(md)
    assert result.matched is True


def test_frontmatter_close_after_201_lines_unmatched():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    header = "\n".join(f"key{i}: v" for i in range(201))
    md = "---\n" + header + "\n---\n正文"
    result = parse_frontmatter_safe(md)
    assert result.matched is False


def test_frontmatter_header_over_64kb_unmatched():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\nkey: " + "x" * (70 * 1024) + "\n---\n正文"
    result = parse_frontmatter_safe(md)
    assert result.matched is False


def test_frontmatter_crlf_matches():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\r\ntitle: x\r\n---\r\n正文"
    result = parse_frontmatter_safe(md)
    assert result.matched is True
    assert result.metadata["title"] == "x"
    assert result.body == "正文"


def test_frontmatter_body_middle_separator_preserved():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\ntitle: x\n---\n段落一\n\n---\n\n段落二"
    result = parse_frontmatter_safe(md)
    assert result.matched is True
    assert "---" in result.body


# ---------------------------------------------------------------------------
# ACL fail-closed 边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rf", [0, 1, "false", "true", None])
def test_acl_from_source_acl_non_bool_resolve_failed_fail_closed(rf):
    acl = SourceACLView.from_source_acl({"scope": "s1", "groups": [], "resolve_failed": rf})
    assert acl.is_fail_closed


def test_acl_from_source_acl_scope_non_str_fail_closed():
    acl = SourceACLView.from_source_acl({"scope": 123, "groups": [], "resolve_failed": False})
    assert acl.is_fail_closed


@pytest.mark.parametrize("groups", ["eng", 123, {1, 2}, {"nested": "set"}])
def test_acl_from_source_acl_invalid_groups_fail_closed(groups):
    acl = SourceACLView.from_source_acl({"scope": "s1", "groups": groups, "resolve_failed": False})
    assert acl.is_fail_closed


def test_acl_from_source_acl_mappingproxy_input():
    from types import MappingProxyType
    acl = SourceACLView.from_source_acl(MappingProxyType({
        "scope": "s1", "groups": ["eng"], "resolve_failed": False,
    }))
    assert acl.is_resolved
    assert acl.groups == frozenset({"eng"})


@pytest.mark.parametrize("rf", [0, 1, "false", "true", None])
def test_acl_direct_construct_non_bool_resolve_failed_rejected(rf):
    with pytest.raises(ValueError):
        SourceACLView(scope="s1", groups=frozenset({"eng"}), resolve_failed=rf)  # type: ignore[arg-type]


def test_acl_has_no_to_source_acl_dict():
    """删除伪造 raw 的 to_source_acl_dict。"""
    assert not hasattr(SourceACLView, "to_source_acl_dict")


# ---------------------------------------------------------------------------
# from_dict 严格化
# ---------------------------------------------------------------------------


def test_from_dict_acl_none_rejected():
    with pytest.raises(ValueError):
        RawSourceItem.from_dict({**_item().to_dict(), "acl": None})


def test_from_dict_acl_list_rejected():
    with pytest.raises(ValueError):
        RawSourceItem.from_dict({**_item().to_dict(), "acl": []})
    with pytest.raises(ValueError):
        CanonicalNote.from_dict({**_valid_cnote().to_dict(), "acl": []})


def test_from_dict_diagnostics_invalid_types_rejected():
    with pytest.raises(ValueError):
        CanonicalNote.from_dict({**_valid_cnote().to_dict(), "diagnostics": ""})
    with pytest.raises(ValueError):
        CanonicalNote.from_dict({**_valid_cnote().to_dict(), "diagnostics": {}})


def test_from_dict_missing_acl_defaults_fail_closed():
    d = _item().to_dict()
    d.pop("acl")
    item = RawSourceItem.from_dict(d)
    assert item.acl.is_fail_closed


# ---------------------------------------------------------------------------
# output_kind 契约
# ---------------------------------------------------------------------------


def test_registry_rejects_output_kind_unknown():
    from app.core.source_conversion.registry import SourceConverterRegistry

    class _U:
        key = "u"
        version = "v1"
        priority = 5
        output_kind = ContentKind.UNKNOWN

        def match(self, item):
            return None

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.UNKNOWN)

    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_U())


def test_registry_rejects_string_output_kind():
    """Phase 1.5：output_kind 严格要求 ContentKind 实例，字符串 "text" 拒绝。"""
    from app.core.source_conversion.registry import SourceConverterRegistry

    class _S:
        key = "s"
        version = "v1"
        priority = 5
        output_kind = "text"  # 字符串枚举值 → 拒绝

        def match(self, item):
            return None

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT)

    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_S())


def test_output_kind_unknown_output_fails_through_service():
    class _UOut:
        key = "uo"
        version = "v1"
        priority = 5
        output_kind = ContentKind.TEXT

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.UNKNOWN)

    svc = _svc()
    svc.register(_UOut())
    note = svc.convert(_item(ext="u1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)
    assert note.conversion_metadata["selected_match"]["converter_key"] == "uo"


def test_output_kind_declared_markdown_returns_unknown_fails():
    class _M:
        key = "md"
        version = "v1"
        priority = 5
        output_kind = ContentKind.MARKDOWN

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.UNKNOWN)

    svc = _svc()
    svc.register(_M())
    note = svc.convert(_item(ext="m1"))
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)


# ---------------------------------------------------------------------------
# 源代码语言解析
# ---------------------------------------------------------------------------


def test_source_code_mime_javascript_no_ext():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("console.log(1)"),
        filename="script", extension="", mime_type="application/javascript",
        content_kind=ContentKind.CODE,
    )
    note = CanonicalNoteService().convert(item)
    assert note.body.startswith("```javascript\n")


def test_source_code_mime_python_no_ext():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("print(1)"),
        filename="s", extension="", mime_type="text/x-python",
        content_kind=ContentKind.CODE,
    )
    note = CanonicalNoteService().convert(item)
    assert note.body.startswith("```python\n")


def test_source_code_mime_json_no_ext():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload('{"a": 1}'),
        filename="d", extension="", mime_type="application/json",
        content_kind=ContentKind.CODE,
    )
    note = CanonicalNoteService().convert(item)
    assert note.body.startswith("```json\n")


def test_source_code_content_kind_code_with_extension_conflict():
    """content_kind=code + .py + application/json → MIME(3) 优先，json 语言。"""
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload('{"a": 1}'),
        filename="a.py", extension="py", mime_type="application/json",
        content_kind=ContentKind.CODE,
    )
    note = CanonicalNoteService().convert(item)
    assert note.body.startswith("```json\n")


def test_source_code_resolved_language_in_metadata():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("print(1)"),
        filename="a.py", extension="py", content_kind=ContentKind.CODE,
    )
    note = CanonicalNoteService().convert(item)
    assert note.conversion_metadata["code_lang"] == "python"


# ---------------------------------------------------------------------------
# TEXT_PAYLOAD 最低证据
# ---------------------------------------------------------------------------


def test_text_payload_no_format_info_uses_text_payload():
    """纯 SourceTextPayload 无格式信息 → converted/text（最低安全降级）。"""
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("hello"),
        filename="data", extension="", mime_type="",
    )
    note = CanonicalNoteService().convert(item)
    assert note.converter_key == "text"
    assert note.conversion_status == ConversionStatus.CONVERTED
    assert note.body == "hello"


def test_bytes_payload_no_format_info_blocked():
    """SourceBytesPayload 不得仅凭 payload 类型进入文本兜底。"""
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"hello"),
        filename="data", extension="", mime_type="",
    )
    note = CanonicalNoteService().convert(item)
    assert note.conversion_status == ConversionStatus.BLOCKED


# ---------------------------------------------------------------------------
# 对象层类型不变量
# ---------------------------------------------------------------------------


def test_direct_construct_content_kind_invalid():
    for bad in (123, True, object()):
        with pytest.raises(ValueError):
            RawSourceItem(source_type="t", external_id="x",
                          payload=source_text_payload("hi"), content_kind=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            CanonicalNote(
                body="x", identity=SourceIdentity(source_type="t", source_id="x"),
                source_hash=_SHA, converter_key="k", converter_version="v1",
                conversion_status=ConversionStatus.FAILED,
                diagnostics=(ConversionDiagnostic(code="e", severity=DiagnosticSeverity.ERROR),),
                content_kind=bad,  # type: ignore[arg-type]
            )


def test_direct_construct_severity_invalid():
    for bad in (123, True, object()):
        with pytest.raises(ValueError):
            ConversionDiagnostic(code="c", severity=bad)  # type: ignore[arg-type]


def test_direct_construct_str_fields_invalid():
    for f in ("filename", "source_url", "source_path", "external_version", "source_updated_at"):
        with pytest.raises(ValueError):
            RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"),
                          **{f: 123})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 对象关系（ConverterSelection）
# ---------------------------------------------------------------------------


def test_selection_converter_key_mismatch_rejected():
    from app.core.source_conversion.base import ConverterSelection
    m = ConverterMatch("other", EvidenceStrength.EXTENSION, reason="x")
    with pytest.raises(ValueError):
        ConverterSelection(selected_converter=_FakeConv("a"), selected_match=m,
                           candidate_matches=(m,))


def test_selection_selected_match_not_in_candidates_rejected():
    from app.core.source_conversion.base import ConverterSelection
    m_sel = ConverterMatch("a", EvidenceStrength.EXTENSION, reason="x")
    m_other = ConverterMatch("b", EvidenceStrength.EXTENSION, reason="y")
    with pytest.raises(ValueError):
        ConverterSelection(selected_converter=_FakeConv("a"), selected_match=m_sel,
                           candidate_matches=(m_other,))


def test_selection_duplicate_candidates_rejected():
    from app.core.source_conversion.base import ConverterSelection
    m_sel = ConverterMatch("a", EvidenceStrength.EXTENSION, reason="x")
    with pytest.raises(ValueError):
        ConverterSelection(selected_converter=_FakeConv("a"), selected_match=m_sel,
                           candidate_matches=(m_sel, m_sel))


# ---------------------------------------------------------------------------
# 对象层 reason 非空
# ---------------------------------------------------------------------------


def test_converter_match_blank_reason_rejected():
    with pytest.raises(ValueError):
        ConverterMatch("a", EvidenceStrength.EXTENSION, reason="   ")


# ---------------------------------------------------------------------------
# register 校验：match/convert callable
# ---------------------------------------------------------------------------


def test_register_missing_convert_rejected():
    from app.core.source_conversion.registry import SourceConverterRegistry

    class _NoConvert:
        key = "nc"
        version = "v1"
        priority = 5
        output_kind = ContentKind.TEXT

        def match(self, item):
            return None

    # convert 缺失 → register 应拒绝（用 getattr 检查 callable）
    try:
        SourceConverterRegistry().register(_NoConvert())
        # 若上面未抛，说明本实现未校验 convert callable —— 改为直接验证 match/convert 属性存在
    except ValueError:
        return
    raise AssertionError("缺少 convert 的 converter 应被拒绝")


def test_register_missing_match_rejected():
    from app.core.source_conversion.registry import SourceConverterRegistry

    class _NoMatch:
        key = "nm"
        version = "v1"
        priority = 5
        output_kind = ContentKind.TEXT

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT)

    try:
        SourceConverterRegistry().register(_NoMatch())
    except ValueError:
        return
    raise AssertionError("缺少 match 的 converter 应被拒绝")
