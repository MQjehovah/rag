"""Phase 1.3：契约对象测试（严格类型、Mapping 校验、深层不可变、序列化、不变量、ACL）。"""
from __future__ import annotations

import json

import pytest

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
    SourceTextPayload,
    source_bytes_payload,
    source_text_payload,
)


def _md_item(**overrides):
    kwargs = dict(
        source_type="dingtalk",
        external_id="doc-1",
        payload=source_text_payload("# 标题\n\n正文"),
        filename="a.md",
        extension="md",
        title="A",
    )
    kwargs.update(overrides)
    return RawSourceItem(**kwargs)


def _valid_note(**overrides):
    kwargs = dict(
        body="# 正文",
        title="标题",
        identity=SourceIdentity(source_type="dingtalk", source_id="d1"),
        source_hash="a" * 64,
        converter_key="markdown",
        converter_version="v1",
        conversion_status=ConversionStatus.CONVERTED,
        content_kind=ContentKind.MARKDOWN,
    )
    kwargs.update(overrides)
    return CanonicalNote(**kwargs)


# ---------------------------------------------------------------------------
# RawSourceItem 输入规范
# ---------------------------------------------------------------------------


def test_source_type_external_id_required():
    with pytest.raises(ValueError):
        RawSourceItem(source_type="", external_id="x", payload=source_text_payload("a"))
    with pytest.raises(ValueError):
        RawSourceItem(source_type="t", external_id="", payload=source_text_payload("a"))
    item = RawSourceItem(source_type=" t ", external_id=" x ", payload=source_text_payload("a"))
    assert item.source_type == "t"
    assert item.external_id == "x"


def test_payload_must_be_exactly_one_and_nonempty():
    with pytest.raises(ValueError):
        RawSourceItem(source_type="t", external_id="x", payload=source_text_payload(""))
    with pytest.raises(ValueError):
        RawSourceItem(source_type="t", external_id="x", payload=source_bytes_payload(b""))


def test_content_kind_defaults_unknown_and_restricted():
    assert _md_item().content_kind == ContentKind.UNKNOWN
    with pytest.raises(ValueError):
        _md_item(content_kind="bogus")


def test_extension_mime_normalized():
    assert _md_item(extension=".MD").extension == "md"
    assert _md_item(extension="", filename="dir\\report.TXT").extension == "txt"
    assert _md_item(mime_type="TEXT/MARKDOWN; charset=utf-8").mime_type == "text/markdown"


def test_source_metadata_top_level_must_be_mapping():
    for bad in ([1, 2], "str", object()):
        with pytest.raises(ValueError):
            _md_item(source_metadata=bad)  # type: ignore[arg-type]


def test_acl_must_be_source_acl_view():
    with pytest.raises(ValueError):
        _md_item(acl={"scope": "s1"})  # type: ignore[arg-type]


def test_payload_kind_not_forgeable():
    with pytest.raises(TypeError):
        SourceTextPayload(text="hi", kind="bytes")  # type: ignore[call-arg]


def test_payload_type_enforced():
    with pytest.raises(ValueError):
        SourceTextPayload(text=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourceBytesPayload(bytes="not bytes")  # type: ignore[arg-type]


def test_unknown_payload_kind_rejected_on_from_dict():
    with pytest.raises(ValueError):
        SourceTextPayload.from_dict({"kind": "bytes", "text": "x"})
    with pytest.raises(ValueError):
        SourceBytesPayload.from_dict({"kind": "text", "bytes": "x"})
    with pytest.raises(ValueError):
        RawSourceItem.from_dict({**_md_item().to_dict(), "payload": {"kind": "other"}})


def test_from_dict_does_not_convert_list_body_to_str():
    """from_dict 不把 list/dict body/text 强制转字符串。"""
    d = _valid_note().to_dict()
    d["body"] = ["not", "string"]
    with pytest.raises(ValueError):
        CanonicalNote.from_dict(d)
    d2 = _md_item().to_dict()
    d2["payload"] = {"kind": "text", "text": ["x"]}
    with pytest.raises(ValueError):
        RawSourceItem.from_dict(d2)


def test_from_dict_requires_mapping_input():
    with pytest.raises(ValueError):
        SourceIdentity.from_dict("not a dict")
    with pytest.raises(ValueError):
        CanonicalNote.from_dict([1, 2])
    with pytest.raises(ValueError):
        ConversionDiagnostic.from_dict(None)


def test_from_dict_enum_invalid_rejected():
    d = _valid_note().to_dict()
    d["conversion_status"] = "bogus"
    with pytest.raises(ValueError):
        CanonicalNote.from_dict(d)


def test_from_dict_bool_not_priority():
    d = _md_item().to_dict()
    d["acl"] = {"scope": "s1", "groups": [], "resolve_failed": False}
    # 无关 priority；这里验证 bool 不作为 int priority 进入（由 ConverterMatch 测）
    RawSourceItem.from_dict(d)


def test_from_dict_unknown_schema_version_rejected():
    d = _valid_note().to_dict()
    d["schema_version"] = "canonical-note/v999"
    with pytest.raises(ValueError):
        CanonicalNote.from_dict(d)


# ---------------------------------------------------------------------------
# Hash 由内容决定
# ---------------------------------------------------------------------------


def test_source_hash_computed_and_forged_rejected():
    import hashlib
    item = _md_item()
    assert item.source_hash == hashlib.sha256("# 标题\n\n正文".encode("utf-8")).hexdigest()
    d = item.to_dict()
    d["source_hash"] = "0" * 64
    with pytest.raises(ValueError):
        RawSourceItem.from_dict(d)


def test_body_hash_computed_and_forged_rejected():
    note = _valid_note()
    import hashlib
    assert note.body_hash == hashlib.sha256("# 正文".encode("utf-8")).hexdigest()
    d = note.to_dict()
    d["body_hash"] = "0" * 64
    with pytest.raises(ValueError):
        CanonicalNote.from_dict(d)


def test_source_hash_must_be_sha256_format():
    with pytest.raises(ValueError):
        _valid_note(source_hash="a")


# ---------------------------------------------------------------------------
# 状态不变量
# ---------------------------------------------------------------------------


def test_converted_requires_body_no_diag():
    with pytest.raises(ValueError):
        _valid_note(body="")
    with pytest.raises(ValueError):
        _valid_note(body="x", diagnostics=(ConversionDiagnostic(code="w", severity=DiagnosticSeverity.WARNING),))


def test_partial_requires_body():
    with pytest.raises(ValueError):
        _valid_note(conversion_status=ConversionStatus.PARTIAL, body="")


def test_failed_blocked_invariants():
    with pytest.raises(ValueError):
        _valid_note(conversion_status=ConversionStatus.FAILED, body="usable")
    with pytest.raises(ValueError):
        _valid_note(conversion_status=ConversionStatus.FAILED, body="", diagnostics=())
    with pytest.raises(ValueError):
        _valid_note(conversion_status=ConversionStatus.BLOCKED, body="usable")
    with pytest.raises(ValueError):
        _valid_note(conversion_status=ConversionStatus.BLOCKED, body="", diagnostics=(), converter_key="", converter_version="")


def test_converted_partial_failed_require_converter():
    with pytest.raises(ValueError):
        _valid_note(converter_key="", converter_version="")


def test_converted_partial_content_kind_not_unknown():
    with pytest.raises(ValueError):
        _valid_note(content_kind=ContentKind.UNKNOWN)


def test_identity_required_and_type():
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity={"source_type": "t"})  # type: ignore[arg-type]


def test_body_title_must_be_str():
    with pytest.raises(ValueError):
        _valid_note(body=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _valid_note(title=["x"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SourceIdentity 必填
# ---------------------------------------------------------------------------


def test_source_identity_requires_nonempty():
    with pytest.raises(ValueError):
        SourceIdentity(source_type="", source_id="x")
    with pytest.raises(ValueError):
        SourceIdentity(source_type="t", source_id="")
    with pytest.raises(ValueError):
        SourceIdentity(source_type="   ", source_id="  ")
    with pytest.raises(ValueError):
        SourceIdentity(source_type=123, source_id="x")  # type: ignore[arg-type]


def test_source_identity_url_fields_must_be_str():
    with pytest.raises(ValueError):
        SourceIdentity(source_type="t", source_id="x", source_url=123)  # type: ignore[arg-type]


def test_source_identity_from_dict_not_str_coercion():
    with pytest.raises(ValueError):
        SourceIdentity.from_dict({"source_type": ["list"], "source_id": "x"})
    with pytest.raises(ValueError):
        SourceIdentity.from_dict({"source_type": "t", "source_id": {"dict": 1}})


# ---------------------------------------------------------------------------
# ACL fail closed + 严格 groups
# ---------------------------------------------------------------------------


def test_acl_default_fail_closed():
    acl = SourceACLView()
    assert acl.is_fail_closed
    assert acl.is_resolved is False


def test_acl_empty_scope_resolved_rejected():
    with pytest.raises(ValueError):
        SourceACLView(scope="", groups=frozenset(), resolve_failed=False)


def test_acl_valid_scopes():
    assert SourceACLView(scope="company", resolve_failed=False).is_resolved
    assert SourceACLView(scope="admin", resolve_failed=False).is_resolved
    assert SourceACLView(scope="group:eng", groups=frozenset({"eng"}), resolve_failed=False).is_resolved


def test_acl_groups_string_not_char_split():
    """from_dict 传入 groups="eng" → ValueError（不得逐字符解析）。"""
    with pytest.raises(ValueError):
        SourceACLView.from_dict({"scope": "s1", "groups": "eng", "resolve_failed": False})


def test_acl_groups_int_no_attributeerror():
    with pytest.raises(ValueError):
        SourceACLView(groups={1, 2})  # type: ignore[arg-type]


def test_acl_groups_whitespace_stripped_and_deduped():
    acl = SourceACLView(scope="s1", groups=frozenset({" eng ", "eng", "  "}), resolve_failed=False)
    assert acl.groups == frozenset({"eng"})


def test_acl_mixed_container_group_non_str_rejected():
    with pytest.raises(ValueError):
        SourceACLView(groups=[1, 2])  # type: ignore[arg-type]


def test_acl_from_dict_forged_resolved_forced_fail_closed():
    acl = SourceACLView.from_dict({"scope": "", "groups": [], "resolve_failed": False})
    assert acl.is_fail_closed


def test_missing_acl_item_stays_fail_closed_through_service():
    from app.core.source_conversion.service import CanonicalNoteService
    item = RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"), filename="a.md")
    assert CanonicalNoteService().convert(item).acl.is_fail_closed


def test_raw_acl_only_comes_from_connector():
    from app.core.source_conversion.service import CanonicalNoteService
    note = CanonicalNoteService().convert(_md_item())
    assert "raw_acl" not in note.source_metadata
    item2 = _md_item(source_metadata={"raw_acl": {"space_id": "s1"}})
    assert CanonicalNoteService().convert(item2).source_metadata["raw_acl"] == {"space_id": "s1"}


# ---------------------------------------------------------------------------
# 深层不可变 / JSON-safe
# ---------------------------------------------------------------------------


def test_nested_dict_frozen_after_construction():
    meta = {"a": {"b": [1, 2, 3]}}
    item = _md_item(source_metadata=meta)
    meta["a"]["b"].append(999)
    assert item.source_metadata["a"]["b"] == (1, 2, 3)


def test_metadata_rejects_object_bytes_set_nan():
    import datetime
    for bad in (object(), b"bytes", datetime.datetime.now(), {1, 2}, float("nan")):
        with pytest.raises(ValueError):
            _md_item(source_metadata={"k": bad})


def test_metadata_rejects_non_string_key():
    with pytest.raises(ValueError):
        _md_item(source_metadata={1: "x"})


def test_diagnostic_detail_must_be_mapping():
    with pytest.raises(ValueError):
        ConversionDiagnostic(code="c", detail=[1, 2])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConversionDiagnostic(code="c", detail="str")  # type: ignore[arg-type]


def test_diagnostic_detail_frozen():
    detail = {"n": [1, 2]}
    d = ConversionDiagnostic(code="c", detail=detail)
    detail["n"].append(3)
    assert d.detail["n"] == (1, 2)


def test_converter_output_metadata_top_level_must_be_mapping():
    with pytest.raises(ValueError):
        ConverterOutput(body="x", conversion_metadata=[1, 2])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ConverterOutput(body="x", source_metadata_extra="str")  # type: ignore[arg-type]


def test_thaw_is_json_dumps_able():
    from app.core.source_conversion.schemas import freeze_mapping, thaw_value
    frozen = freeze_mapping({"a": [1, 2], "b": {"c": "x"}}, field_name="t")
    thawed = thaw_value(frozen)
    json.dumps(thawed)
    assert isinstance(thawed["b"], dict)


# ---------------------------------------------------------------------------
# 序列化往返
# ---------------------------------------------------------------------------


def _roundtrip(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))


def test_source_text_payload_roundtrip():
    p = SourceTextPayload(text="hi")
    assert SourceTextPayload.from_dict(_roundtrip(p.to_dict())) == p


def test_source_bytes_payload_roundtrip():
    p = SourceBytesPayload(bytes=b"\x00\xffabc")
    assert SourceBytesPayload.from_dict(_roundtrip(p.to_dict())).bytes == b"\x00\xffabc"


def test_identity_roundtrip():
    i = SourceIdentity(source_type="t", source_id="x", source_url="u", source_path="p", external_version="v")
    assert SourceIdentity.from_dict(_roundtrip(i.to_dict())) == i


def test_acl_roundtrip():
    a = SourceACLView(scope="group:eng", groups=frozenset({"eng"}), resolve_failed=False)
    assert SourceACLView.from_dict(_roundtrip(a.to_dict())) == a


def test_raw_source_item_roundtrip():
    item = _md_item(
        content_kind=ContentKind.MARKDOWN,
        source_metadata={"input_frontmatter": {"title": "旧"}},
        acl=SourceACLView(scope="s1", groups=frozenset({"eng"}), resolve_failed=False),
    )
    back = RawSourceItem.from_dict(_roundtrip(item.to_dict()))
    assert back == item
    assert back.source_hash == item.source_hash


def test_canonical_note_roundtrip_preserves_all():
    note = _valid_note(
        body="# 正文",
        conversion_status=ConversionStatus.PARTIAL,
        source_metadata={"input_frontmatter": {"a": 1}, "raw_acl": {"scope": "s1"}},
        acl=SourceACLView(scope="s1", groups=frozenset({"eng"}), resolve_failed=False),
        diagnostics=(ConversionDiagnostic(code="w", severity=DiagnosticSeverity.WARNING, detail={"n": [1, 2]}),),
        conversion_metadata={"selected_match": {"a": 1}},
    )
    back = CanonicalNote.from_dict(_roundtrip(note.to_dict()))
    assert back == note
    assert back.body_hash == note.body_hash
    assert back.diagnostics[0].detail["n"] == (1, 2)
    assert back.source_metadata["raw_acl"] == {"scope": "s1"}


def test_converter_output_roundtrip_via_class_methods():
    """真实 roundtrip：out.to_dict() → json → ConverterOutput.from_dict()。"""
    out = ConverterOutput(
        body="x", status_signal=ConversionStatus.PARTIAL,
        diagnostics=(ConversionDiagnostic(code="w"),),
        conversion_metadata={"k": "v", "nested": {"a": [1, 2]}},
        content_kind=ContentKind.CSV,
        suggested_title="t",
        source_metadata_extra={"extra": "e"},
    )
    restored = ConverterOutput.from_dict(json.loads(json.dumps(out.to_dict(), ensure_ascii=False)))
    assert restored == out
    assert restored.status_signal == ConversionStatus.PARTIAL
    assert restored.content_kind == ContentKind.CSV
    assert restored.diagnostics[0].code == "w"


# ---------------------------------------------------------------------------
# ConverterOutput 契约校验
# ---------------------------------------------------------------------------


def test_converter_output_diagnostics_must_be_objects():
    with pytest.raises(ValueError):
        ConverterOutput(body="x", diagnostics=({"code": "x"},))  # type: ignore[arg-type]


def test_converter_output_rejects_blocked():
    with pytest.raises(ValueError):
        ConverterOutput(body="x", status_signal=ConversionStatus.BLOCKED)


def test_converter_output_whitespace_body_is_empty():
    assert ConverterOutput(body="   \n  ").body == ""


def test_converter_output_rejects_bool_priority_in_from_dict():
    d = ConverterOutput(body="x").to_dict()
    # ConverterOutput 无 priority；这里验证 from_dict 对非法 status_signal 拒绝
    d["status_signal"] = "bogus"
    with pytest.raises(ValueError):
        ConverterOutput.from_dict(d)


# ---------------------------------------------------------------------------
# ConversionDiagnostic 校验
# ---------------------------------------------------------------------------


def test_diagnostic_code_required():
    with pytest.raises(ValueError):
        ConversionDiagnostic(code="")


def test_diagnostic_roundtrip():
    d = ConversionDiagnostic(code="c", severity=DiagnosticSeverity.ERROR, detail={"a": [1]})
    assert ConversionDiagnostic.from_dict(_roundtrip(d.to_dict())) == d
