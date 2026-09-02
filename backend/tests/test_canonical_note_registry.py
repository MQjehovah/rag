"""Phase 1.3：Registry 确定性选择、fail closed、生命周期、Match 校验、证据矩阵。"""
from __future__ import annotations

import pytest

from app.core.source_conversion.base import (
    AmbiguousConverterError,
    ConverterMatch,
    ConverterMatchError,
    EvidenceStrength,
)
from app.core.source_conversion.registry import SourceConverterRegistry
from app.core.source_conversion.schemas import (
    ContentKind,
    ConverterOutput,
    RawSourceItem,
    source_bytes_payload,
    source_text_payload,
)


class _FakeConverter:
    """可注入 Converter。match 行为由构造参数决定。"""

    def __init__(self, key: str, version: str = "test", priority: int = 0,
                 match: bool = True, specificity: EvidenceStrength = EvidenceStrength.EXTENSION,
                 reason: str = "fake", raise_on_match: bool = False,
                 fake_key: str | None = None, output_kind: ContentKind = ContentKind.TEXT,
                 return_dict: bool = False):
        self.key = key
        self.version = version
        self.priority = priority
        self.output_kind = output_kind
        self._match = match
        self._specificity = specificity
        self._reason = reason
        self._raise_on_match = raise_on_match
        self._fake_key = fake_key
        self._return_dict = return_dict

    def match(self, item: RawSourceItem):
        if self._raise_on_match:
            raise RuntimeError("match exploded")
        if not self._match:
            return None
        if self._return_dict:
            return {"converter_key": self.key}  # type: ignore[return-value]
        return ConverterMatch(
            converter_key=self._fake_key or self.key,
            specificity=self._specificity,
            reason=self._reason,
        )

    def convert(self, item, selected_match):
        return ConverterOutput(body=f"[{self.key}]", content_kind=self.output_kind)


def _item(**kwargs):
    base = dict(
        source_type="t", external_id="x",
        payload=source_text_payload("hello"),
        extension="bin",
    )
    base.update(kwargs)
    return RawSourceItem(**base)


# ---------------------------------------------------------------------------
# 确定性选择（ConverterSelection）
# ---------------------------------------------------------------------------


def test_single_hit_selected():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a"))
    reg.register(_FakeConverter("b", match=False))
    selection = reg.select(_item())
    assert selection is not None
    assert selection.selected_converter.key == "a"
    assert selection.selected_match.converter_key == "a"
    assert len(selection.candidate_matches) == 1


def test_no_hit_returns_none():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a", match=False))
    assert reg.select(_item()) is None


def test_higher_priority_wins():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("low", priority=1))
    reg.register(_FakeConverter("high", priority=10))
    assert reg.select(_item()).selected_match.converter_key == "high"


def test_higher_specificity_wins_even_lower_priority():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("ext", priority=10, specificity=EvidenceStrength.EXTENSION))
    reg.register(_FakeConverter("magic", priority=1, specificity=EvidenceStrength.FILE_HEADER))
    assert reg.select(_item()).selected_match.converter_key == "magic"


def test_ambiguous_raises_with_candidates():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a", priority=5))
    reg.register(_FakeConverter("b", priority=5))
    with pytest.raises(AmbiguousConverterError) as excinfo:
        reg.select(_item())
    assert excinfo.value.candidates


def test_registration_order_does_not_affect_selection():
    def build(order):
        reg = SourceConverterRegistry()
        for key in order:
            spec = EvidenceStrength.FILE_HEADER if key == "pdf" else EvidenceStrength.EXTENSION
            reg.register(_FakeConverter(key, priority=5, specificity=spec))
        return reg

    item = _item()
    assert build(["pdf", "office"]).select(item).selected_match.converter_key == "pdf"
    assert build(["office", "pdf"]).select(item).selected_match.converter_key == "pdf"


def test_keys_stable_sorted():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("z"))
    reg.register(_FakeConverter("a"))
    assert reg.keys() == ["a", "z"]


def test_candidate_matches_deterministically_sorted():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("aa", priority=1, specificity=EvidenceStrength.EXTENSION))
    reg.register(_FakeConverter("z", priority=5, specificity=EvidenceStrength.EXTENSION))
    reg.register(_FakeConverter("m", priority=3, specificity=EvidenceStrength.MIME))
    selection = reg.select(_item())
    assert [m.converter_key for m in selection.candidate_matches] == ["m", "z", "aa"]


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


def test_duplicate_key_rejected_by_default():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a"))
    with pytest.raises(ValueError):
        reg.register(_FakeConverter("a"))


def test_duplicate_key_replace_allowed():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a", match=True))
    reg.register(_FakeConverter("a", match=False), replace=True)
    assert reg.select(_item()) is None


def test_missing_key_or_version_rejected():
    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_FakeConverter("", version="v1"))
    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_FakeConverter("a", version=""))


def test_register_requires_int_priority_not_bool():
    class _BadPrio:
        key = "bp"
        version = "v1"
        priority = True  # bool 冒充 int
        output_kind = ContentKind.TEXT

        def match(self, item):
            return None

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT)

    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_BadPrio())


def test_register_requires_output_kind():
    class _NoKind:
        key = "nk"
        version = "v1"
        priority = 5

        def match(self, item):
            return None

        def convert(self, item, selected_match):
            return ConverterOutput(body="x")

    with pytest.raises(ValueError):
        SourceConverterRegistry().register(_NoKind())


def test_seal_blocks_registration():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a"))
    reg.seal()
    assert reg.sealed
    with pytest.raises(RuntimeError):
        reg.register(_FakeConverter("b"))
    assert reg.select(_item()) is not None


# ---------------------------------------------------------------------------
# fail closed（严格）
# ---------------------------------------------------------------------------


def test_match_exception_raises_converter_match_error():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("boom", raise_on_match=True, priority=10))
    reg.register(_FakeConverter("text_fallback", match=True, priority=1))
    with pytest.raises(ConverterMatchError) as excinfo:
        reg.select(_item())
    assert excinfo.value.converter_key == "boom"


def test_match_returning_dict_raises_converter_match_error():
    """返回 dict 而不是 ConverterMatch → fail closed（不崩溃、不回退）。"""
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("bad", return_dict=True, priority=10))
    reg.register(_FakeConverter("fallback", match=True, priority=1))
    with pytest.raises(ConverterMatchError) as excinfo:
        reg.select(_item())
    assert excinfo.value.error_type == "non_converter_match"


def test_forged_converter_key_raises_not_ignored():
    """伪造 converter_key → fail closed（不忽略、不回退）。"""
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a", fake_key="nonexistent"))
    reg.register(_FakeConverter("b", match=False))
    with pytest.raises(ConverterMatchError) as excinfo:
        reg.select(_item())
    assert excinfo.value.error_type == "forged_converter_key"


def test_empty_reason_raises():
    # 对象层：空 reason 直接拒绝
    with pytest.raises(ValueError):
        ConverterMatch("a", EvidenceStrength.EXTENSION, reason="  ")
    # Registry 层：match 构造失败（空 reason 抛 ValueError）→ fail closed
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a", reason="  "))
    reg.register(_FakeConverter("fallback", match=True, priority=1))
    with pytest.raises(ConverterMatchError) as excinfo:
        reg.select(_item())
    assert excinfo.value.converter_key == "a"


def test_match_priority_from_converter_not_match():
    """priority 由 Registry 从 Converter.priority 获取，Match 不能伪造。"""

    class _PrioFaker:
        key = "pf"
        version = "v1"
        priority = 77
        output_kind = ContentKind.TEXT

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, priority=999, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=ContentKind.TEXT)

    reg = SourceConverterRegistry()
    reg.register(_PrioFaker())
    assert reg.select(_item()).selected_match.priority == 77


def test_match_evidence_deep_frozen():
    evidence = {"nested": [1, 2]}
    m = ConverterMatch("a", EvidenceStrength.EXTENSION, reason="x", evidence=evidence)
    evidence["nested"].append(3)
    assert m.evidence["nested"] == (1, 2)
    with pytest.raises(Exception):
        m.evidence["nested"] = (1,)


def test_match_evidence_rejects_set():
    with pytest.raises(ValueError):
        ConverterMatch("a", EvidenceStrength.EXTENSION, reason="x", evidence={"k": {1, 2}})


def test_match_roundtrip():
    import json
    m = ConverterMatch("a", EvidenceStrength.FILE_HEADER, priority=5, reason="r", evidence={"k": 1})
    restored = ConverterMatch.from_dict(json.loads(json.dumps(m.to_dict())))
    assert restored == m
    assert restored.specificity == EvidenceStrength.FILE_HEADER


def test_match_rejects_string_specificity():
    with pytest.raises(ValueError):
        ConverterMatch("a", "5", reason="x")  # type: ignore[arg-type]


def test_match_rejects_bool_priority():
    with pytest.raises(ValueError):
        ConverterMatch("a", EvidenceStrength.EXTENSION, priority=True, reason="x")  # type: ignore[arg-type]


def test_match_rejects_non_mapping_evidence():
    with pytest.raises(ValueError):
        ConverterMatch("a", EvidenceStrength.EXTENSION, reason="x", evidence=[1, 2])  # type: ignore[arg-type]


def test_selected_converter_matches_selected_key():
    reg = SourceConverterRegistry()
    reg.register(_FakeConverter("a"))
    selection = reg.select(_item())
    assert selection.selected_converter.key == selection.selected_match.converter_key == "a"


# ---------------------------------------------------------------------------
# 内置证据矩阵（最强证据）
# ---------------------------------------------------------------------------


def test_builtin_match_matrix_strongest_evidence():
    from app.core.source_conversion.converters import build_builtin_registry

    reg = build_builtin_registry()

    # %PDF + content_kind=pdf + application/pdf + .pdf → FILE_HEADER
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"%PDF-1.4 fake"),
        filename="a.pdf", extension="pdf", mime_type="application/pdf",
        content_kind=ContentKind.PDF,
    )
    sel = reg.select(item)
    assert sel.selected_match.specificity == EvidenceStrength.FILE_HEADER

    # 非 %PDF + .pdf → 扩展名级
    item2 = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"plain"), filename="a.pdf", extension="pdf",
    )
    assert reg.select(item2).selected_match.specificity == EvidenceStrength.EXTENSION

    # .csv + text/tab-separated-values → CSV MIME(3)
    item3 = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_bytes_payload(b"a\tb\n1\t2\n"),
        filename="a.csv", extension="csv", mime_type="text/tab-separated-values",
    )
    sel3 = reg.select(item3)
    assert sel3.selected_match.specificity == EvidenceStrength.MIME

    # .md + text/markdown + content_kind=markdown → CONTENT_KIND(4)
    item4 = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("# m"),
        filename="a.md", extension="md", mime_type="text/markdown",
        content_kind=ContentKind.MARKDOWN,
    )
    assert reg.select(item4).selected_match.specificity == EvidenceStrength.CONTENT_KIND

    # content_kind=text + text/plain → CONTENT_KIND(4)（明确 kind 按统一矩阵优先）
    item5 = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("hello"),
        filename="a.txt", extension="txt", mime_type="text/plain",
        content_kind=ContentKind.TEXT,
    )
    assert reg.select(item5).selected_match.specificity == EvidenceStrength.CONTENT_KIND

    # content_kind=text 与 .py/application/json 冲突 → 明确 kind 优先
    item6 = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("print(1)"),
        filename="a.py", extension="py", mime_type="application/json",
        content_kind=ContentKind.TEXT,
    )
    sel6 = reg.select(item6)
    assert sel6.selected_match.converter_key == "text"  # 明确 kind 优先
    assert sel6.selected_match.specificity == EvidenceStrength.CONTENT_KIND


def test_text_content_kind_evidence_is_4():
    """明确 content_kind=text 证据强度必须为 CONTENT_KIND(4)。"""
    from app.core.source_conversion.converters import build_builtin_registry
    reg = build_builtin_registry()
    item = RawSourceItem(
        source_type="t", external_id="x",
        payload=source_text_payload("hello"),
        filename="f.bin", extension="bin", content_kind=ContentKind.TEXT,
    )
    sel = reg.select(item)
    assert sel.selected_match.specificity == EvidenceStrength.CONTENT_KIND
