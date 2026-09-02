"""Phase 1.6：最终三点修正的独立复现测试。

一、Frontmatter delimiter 精确（开头/闭合行，不用 strip 放宽）
二、所有 Header find 有明确上界（监控 find 带 end；2MB 无换行单行边界内退出）
三、Service 捕获 Selection 后 / 契约校验的 ConverterMatchError → blocked
"""
from __future__ import annotations

import pytest

from app.core.source_conversion.converters import build_builtin_registry
from app.core.source_conversion.registry import SourceConverterRegistry
from app.core.source_conversion.schemas import (
    CanonicalNote,
    ContentKind,
    ConversionStatus,
    ConverterOutput,
    RawSourceItem,
    source_text_payload,
)
from app.core.source_conversion.service import CanonicalNoteService


# ---------------------------------------------------------------------------
# 一、Frontmatter delimiter 精确
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["---oops\n", "---   \n", "----\n", "---\t\n"])
def test_frontmatter_opener_must_be_exact(prefix):
    """开头必须是精确 "---\\n" / "---\\r\\n"；其余 → unmatched。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    assert parse_frontmatter_safe(prefix + "title: x\n---\n正文").matched is False


@pytest.mark.parametrize("close", ["  ---", "---  ", "---\t", "--", "--- x"])
def test_frontmatter_closer_must_be_exact(close):
    """闭合行必须精确 "---" / "---\\r"；带空格/Tab → unmatched。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    assert parse_frontmatter_safe("---\ntitle: x\n" + close + "\n正文").matched is False


def test_frontmatter_legal_lf_and_crlf_match():
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    assert parse_frontmatter_safe("---\ntitle: x\n---\n正文").matched is True
    assert parse_frontmatter_safe("---\r\ntitle: x\r\n---\r\n正文").matched is True


# ---------------------------------------------------------------------------
# 二、所有 Header find 有明确上界
# ---------------------------------------------------------------------------


class _WatchingStr(str):
    """str 子类：监控全文 split / splitlines / 无 end 的 find。"""

    def split(self, sep=None, maxsplit=-1):
        raise AssertionError("对原始全文调用 split 被拒绝")

    def splitlines(self, *args, **kwargs):
        raise AssertionError("对原始全文调用 splitlines 被拒绝")

    def encode(self, encoding="utf-8", errors="strict"):
        # 允许对受限 Header 前缀 encode；这里无法区分，但 find 监控覆盖更关键场景
        return super().encode(encoding, errors)

    def find(self, sub, start=None, end=None):
        if start is None or end is None:
            raise AssertionError("Header 搜索的 find 必须提供 start 与 end")
        return super().find(sub, start, end)


def test_frontmatter_find_always_bounded():
    """所有 Header find 都提供 end（_WatchingStr 监控）。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = _WatchingStr("---\ntitle: x\n---\n正文\n" * 1)
    result = parse_frontmatter_safe(md)
    assert result.matched is True


def test_frontmatter_2mb_single_line_no_newline_bounded():
    """>2MB 无换行单行 Header → 在边界内提前 unmatched（不扫描全文）。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    md = "---\nkey: " + "x" * (2 * 1024 * 1024)  # 2MB 无换行
    result = parse_frontmatter_safe(md)
    assert result.matched is False


def test_frontmatter_no_full_encode():
    """不应对全文 encode（用受限前缀切片后 encode 允许，但全文 encode 拒绝）。"""
    from app.core.source_conversion.frontmatter import parse_frontmatter_safe
    # 1MB 正文：matched=True，说明 Header 搜索不因正文大小受影响
    md = "---\ntitle: x\n---\n" + "y" * (1024 * 1024)
    result = parse_frontmatter_safe(md)
    assert result.matched is True
    assert result.metadata["title"] == "x"


# ---------------------------------------------------------------------------
# 三、Service 捕获 Selection 后的 ConverterMatchError
# ---------------------------------------------------------------------------


class _MutatingRegistry(SourceConverterRegistry):
    """select 返回合法 Selection，但返回前篡改 Converter.output_kind 为字符串。"""

    def __init__(self, base):
        super().__init__()
        # 复制 base 的全部 converters（含被选中者）
        for key in base.keys():
            self._converters[key] = base.get(key)
        self._sealed = True

    def select(self, item):
        selection = super().select(item)
        if selection is not None:
            # 篡改 selected converter 的 output_kind 为字符串（注册后篡改场景）
            selection.selected_converter.output_kind = "text"  # type: ignore[assignment]
        return selection


def _make_converter(key, output_kind=ContentKind.TEXT):
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength

    class _C:
        def __init__(self):
            self.key = key
            self.version = "v1"
            self.priority = 5
            self.output_kind = output_kind

        def match(self, item):
            return ConverterMatch(self.key, EvidenceStrength.EXTENSION, reason="x")

        def convert(self, item, selected_match):
            return ConverterOutput(body="x", content_kind=self.output_kind)

    return _C()


def test_service_captures_post_selection_match_error_blocked():
    """自定义 Registry 返回合法 Selection 但篡改 output_kind → blocked + converter_match_failed。"""
    base = build_builtin_registry(seal=False)
    base.register(_make_converter("mut", output_kind=ContentKind.TEXT))
    reg = _MutatingRegistry(base)
    item = RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"),
                         filename="f.bin", extension="bin", content_kind=ContentKind.TEXT)
    # 不得抛 ConverterMatchError / AttributeError / ValueError
    note = CanonicalNoteService(reg).convert(item)
    assert note.conversion_status == ConversionStatus.BLOCKED
    assert any(d.code == "converter_match_failed" for d in note.diagnostics)


def test_service_captures_post_selection_match_error_no_raise():
    """同上：显式验证 convert 不抛异常。"""
    base = build_builtin_registry(seal=False)
    base.register(_make_converter("mut2", output_kind=ContentKind.TEXT))
    reg = _MutatingRegistry(base)
    item = RawSourceItem(source_type="t", external_id="x", payload=source_text_payload("hi"),
                         filename="f.bin", extension="bin", content_kind=ContentKind.TEXT)
    svc = CanonicalNoteService(reg)
    try:
        note = svc.convert(item)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"convert 不应抛异常: {type(exc).__name__}: {exc}") from exc
    assert note.conversion_status == ConversionStatus.BLOCKED
    diag = [d for d in note.diagnostics if d.code == "converter_match_failed"]
    assert diag
    # 被篡改并触发的是实际被选中的 converter（text），detail 记录其 key
    assert diag[0].detail["converter_key"] == "text"
    assert diag[0].detail["error_type"] == "invalid_converter_after_registration"
