"""Phase 1.3：独立对抗测试（不混入 happy-path 测试）。

覆盖 20 个已复现的契约错误用例，验证 fail closed 行为。
"""
from __future__ import annotations

import json

import pytest

from app.core.source_conversion.base import (
    ConverterMatch,
    EvidenceStrength,
)
from app.core.source_conversion.converters import build_builtin_registry
from app.core.source_conversion.schemas import (
    CanonicalNote,
    ContentKind,
    ConversionDiagnostic,
    ConversionStatus,
    ConverterOutput,
    RawSourceItem,
    SourceACLView,
    SourceIdentity,
    source_bytes_payload,
    source_text_payload,
)
from app.core.source_conversion.service import CanonicalNoteService

_SHA = "a" * 64


def _item(text: str = "hi", ext: str = "bin", **over):
    base = dict(source_type="t", external_id="x", payload=source_text_payload(text), filename=f"a.{ext}", extension=ext)
    base.update(over)
    return RawSourceItem(**base)


def _svc():
    return CanonicalNoteService(build_builtin_registry(seal=False))


# 1. groups="eng" 不会变成字符集合
def test_groups_string_not_char_set():
    with pytest.raises(ValueError):
        SourceACLView.from_dict({"scope": "s1", "groups": "eng", "resolve_failed": False})


# 2. integer group 不产生 AttributeError
def test_integer_group_no_attributeerror():
    with pytest.raises(ValueError):
        SourceACLView(groups=frozenset({123}))  # type: ignore[arg-type]


# 3. 顶层 metadata=list 被拒绝
def test_top_level_metadata_list_rejected():
    with pytest.raises(ValueError):
        RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"),
                      source_metadata=[1, 2])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity=SourceIdentity(source_type="t", source_id="x"),
                      source_hash=_SHA, converter_key="k", converter_version="v1",
                      source_metadata=["x"])  # type: ignore[arg-type]


# 4. diagnostic.detail=list 被拒绝
def test_diagnostic_detail_list_rejected():
    with pytest.raises(ValueError):
        ConversionDiagnostic(code="c", detail=[1, 2])  # type: ignore[arg-type]


# 5. output metadata=list 被拒绝
def test_output_metadata_list_rejected():
    with pytest.raises(ValueError):
        ConverterOutput(body="x", conversion_metadata=[1])  # type: ignore[arg-type]


# 6. match evidence=list 被拒绝
def test_match_evidence_list_rejected():
    with pytest.raises(ValueError):
        ConverterMatch("a", EvidenceStrength.EXTENSION, reason="r", evidence=[1])  # type: ignore[arg-type]


# 7. specificity="5" 被拒绝
def test_specificity_string_rejected():
    with pytest.raises(ValueError):
        ConverterMatch("a", "5", reason="r")  # type: ignore[arg-type]


# 8. 空白 SourceIdentity 被拒绝
def test_blank_source_identity_rejected():
    with pytest.raises(ValueError):
        SourceIdentity(source_type="   ", source_id="  ")


# 9. from_dict 不把 list body/text 转字符串
def test_from_dict_no_list_to_str_coercion():
    d = CanonicalNote(
        body="x", identity=SourceIdentity(source_type="t", source_id="x"),
        source_hash=_SHA, converter_key="k", converter_version="v1",
        content_kind=ContentKind.TEXT,
    ).to_dict()
    d["body"] = ["not", "string"]
    with pytest.raises(ValueError):
        CanonicalNote.from_dict(d)
    d2 = _item().to_dict()
    d2["payload"] = {"kind": "text", "text": [1, 2]}
    with pytest.raises(ValueError):
        RawSourceItem.from_dict(d2)


# 10. match 返回 dict → blocked，不崩溃
def test_match_returning_dict_blocked_not_crash():
    from app.core.source_conversion.base import ConverterMatchError

    class _Bad:
        key = "bd"
        version = "v1"
        priority = 99
        output_kind = ContentKind.TEXT

        def match(self, item):
            return {"converter_key": self.key}  # type: ignore[return-value]

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT)

    svc = _svc()
    svc.register(_Bad())
    note = svc.convert(_item())
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "converter_match_failed" for d in note.diagnostics)


# 11. match 伪造 key → blocked，不回退
def test_match_forged_key_blocked_not_fallback():
    from app.core.source_conversion.base import ConverterMatchError

    class _Forge:
        key = "fg"
        version = "v1"
        priority = 99
        output_kind = ContentKind.TEXT

        def match(self, item):
            return ConverterMatch("nonexistent", EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT)

    svc = _svc()
    svc.register(_Forge())
    # 若静默忽略，会 fallback 到 text converter；正确行为是 blocked
    note = svc.convert(_item())
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "converter_match_failed" for d in note.diagnostics)


# 12. output kind 与 converter.output_kind 不一致 → failed
def test_output_kind_mismatch_failed():
    class _Lying:
        key = "ly"
        version = "v1"
        priority = 5
        output_kind = ContentKind.MARKDOWN

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.PDF)

    svc = _svc()
    svc.register(_Lying())
    note = svc.convert(_item())
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)


# 13. Converter 不能覆盖 selected_match/candidate_matches
def test_converter_cannot_override_selected_match():
    class _Over:
        key = "ov"
        version = "v1"
        priority = 5
        output_kind = ContentKind.TEXT

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT,
                                   conversion_metadata={"selected_match": {"fake": 1}})

    svc = _svc()
    svc.register(_Over())
    note = svc.convert(_item())
    assert note.conversion_status == ConversionStatus.FAILED
    assert any(d.code == "converter_contract_violation" for d in note.diagnostics)


# 14. node shebang + .py → javascript
def test_node_shebang_with_py_outputs_javascript():
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("#!/usr/bin/env node\nconsole.log(1)"),
        filename="app.py", extension="py",
    )
    note = CanonicalNoteService().convert(item)
    assert note.body.startswith("```javascript\n")


# 15. MIME-only Office 能得到正确 extension
def test_office_mime_only_derives_extension():
    from app.core.source_conversion.converters.office import OfficeConverter

    class _Fake:
        seen: list = []

        def convert_bytes(self, content, extension):
            _Fake.seen.append(extension)
            return "# x"

    office = OfficeConverter(markitdown=_Fake())  # type: ignore[arg-type]
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"PK"), extension="", filename="doc",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content_kind=ContentKind.OFFICE,
    )
    match = office.match(item)
    assert match is not None
    assert match.evidence["resolved_format"] == "docx"
    office.convert(item, match)
    assert _Fake.seen[-1] == "docx"


# 16. 嵌套 input_frontmatter 可渲染
def test_nested_input_frontmatter_renderable():
    from app.core.source_conversion.frontmatter import render_frontmatter
    note = CanonicalNote(
        body="正文",
        identity=SourceIdentity(source_type="t", source_id="x"),
        source_hash=_SHA,
        converter_key="markdown", converter_version="v1",
        conversion_status=ConversionStatus.CONVERTED, content_kind=ContentKind.MARKDOWN,
        source_metadata={"input_frontmatter": {"nested": {"a": 1}}},
    )
    rendered = render_frontmatter(note)
    assert "input_frontmatter" in rendered
    # 用 parse_frontmatter 解析，恢复嵌套 dict
    from app.core.source_conversion.frontmatter import parse_frontmatter
    meta, _ = parse_frontmatter(rendered)
    value = json.loads(meta["input_frontmatter"])
    assert value == {"nested": {"a": 1}}


# 17. CSV Converter 只解码一次
def test_csv_single_decode(monkeypatch):
    from app.core.source_conversion.converters import csv_table

    calls = {"n": 0}
    orig = csv_table.decode_text_strict

    def counting(content):
        calls["n"] += 1
        return orig(content)

    monkeypatch.setattr(csv_table, "decode_text_strict", counting)
    note = CanonicalNoteService().convert(_item("a,b\n1,2\n", ext="csv"))
    assert note.body.splitlines()[0] == "| a | b |"
    assert calls["n"] == 1


# 18. content_kind=csv + MIME/extension 冲突 → delimiter 与格式证据一致
def test_csv_content_kind_with_conflicting_evidence():
    # content_kind=csv + TSV MIME + .csv → tab
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a\tb\n1\t2\n"),
        filename="a.csv", extension="csv", mime_type="text/tab-separated-values",
        content_kind=ContentKind.CSV,
    )
    note = CanonicalNoteService().convert(item)
    assert note.conversion_metadata["delimiter"] == "\t"
    assert note.conversion_metadata["resolved_format"] == "tsv"
    # content_kind=csv + CSV MIME + .tsv → comma
    item2 = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a,b\n1,2\n"),
        filename="a.tsv", extension="tsv", mime_type="text/csv",
        content_kind=ContentKind.CSV,
    )
    note2 = CanonicalNoteService().convert(item2)
    assert note2.conversion_metadata["delimiter"] == ","
    assert note2.conversion_metadata["resolved_format"] == "csv"


# 19. 明显二进制 decode_text_strict 返回 ok=False
def test_decode_strict_binary_ok_false():
    from app.core.source_conversion import quality
    binary = b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09" * 20
    text, ok = quality.decode_text_strict(binary)
    assert ok is False
    assert text == ""


# 20. 超长 Frontmatter 不进行无界扫描
def test_overlong_frontmatter_bounded_scan():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    # 超长未闭合 Header（超过 200 行）
    long_md = "---\n" + "\n".join(f"key{i}: value{i}" for i in range(300)) + "\n正文"
    result = parse_frontmatter_safe(long_md)
    assert result.matched is False
    # 超长闭合在限制外（单行超长 Header）
    huge_line = "---\nkey: " + "x" * (70 * 1024) + "\n---\n正文"
    result2 = parse_frontmatter_safe(huge_line)
    assert result2.matched is False


# ---------------------------------------------------------------------------
# 补充对抗：对象层类型检查
# ---------------------------------------------------------------------------


def test_canonical_note_identity_type():
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity="not identity", source_hash=_SHA,
                      converter_key="k", converter_version="v1")  # type: ignore[arg-type]


def test_canonical_note_acl_type():
    with pytest.raises(ValueError):
        CanonicalNote(body="x", identity=SourceIdentity(source_type="t", source_id="x"),
                      acl={"scope": "s1"}, source_hash=_SHA,
                      converter_key="k", converter_version="v1")  # type: ignore[arg-type]


def test_converter_output_from_dict_strict_status():
    d = ConverterOutput(body="x").to_dict()
    d["status_signal"] = "bogus"
    with pytest.raises(ValueError):
        ConverterOutput.from_dict(d)


def test_match_evidence_deep_frozen_immutable():
    evidence = {"nested": {"a": [1]}}
    m = ConverterMatch("a", EvidenceStrength.EXTENSION, reason="r", evidence=evidence)
    evidence["nested"]["a"].append(2)
    assert m.evidence["nested"]["a"] == (1,)
    with pytest.raises(Exception):
        m.evidence["nested"] = {}


def test_output_kind_does_not_leak_into_conversion_metadata():
    note = CanonicalNoteService().convert(_item("# m", ext="md"))
    assert "content_kind" not in note.conversion_metadata
    assert note.content_kind == ContentKind.MARKDOWN


def test_blocked_note_retains_converter_match_failed_detail():
    class _Bad:
        key = "bd2"
        version = "v1"
        priority = 99
        output_kind = ContentKind.TEXT

        def match(self, item):
            raise RuntimeError("secret internal detail")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT)

    svc = _svc()
    svc.register(_Bad())
    note = svc.convert(_item())
    diag = [d for d in note.diagnostics if d.code == "converter_match_failed"][0]
    assert diag.detail["converter_key"] == "bd2"
    assert diag.detail["error_type"] == "RuntimeError"
    # 不暴露原始异常文本
    assert "secret internal detail" not in diag.message
    assert "secret internal detail" not in json.dumps(diag.to_dict())
