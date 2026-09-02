"""Phase 2：Legacy Adapter 纯单元测试。

覆盖 NormalizedSourceItem → RawSourceItem → CanonicalNote 的字段映射、
ACL fail closed、raw_acl 仅来自 Connector、格式确定性判定。
"""
from __future__ import annotations

import pytest

from app.core.source_conversion.schemas import ContentKind, SourceACLView
from app.sources.conversion_adapter import (
    _content_kind_from_ext,
    convert_normalized,
    to_raw_source_item,
)
from app.sources.schemas import NormalizedSourceItem


def _nsi(**over):
    base = dict(
        connection_id="conn-1",
        source_type="gitlab",
        external_id="x1",
        external_version="v1",
        title="t",
        content="hello",
        content_type="text",
        source_path="dir/a.txt",
        source_url="https://x",
        source_updated_at="2026-01-01",
        acl_scope={"scope": "project:1", "groups": [], "resolve_failed": False, "raw": {}},
        metadata_json={"path": "dir/a.txt"},
    )
    base.update(over)
    return NormalizedSourceItem(**base)


# ---------------------------------------------------------------------------
# 字段完整映射
# ---------------------------------------------------------------------------


def test_field_mapping_complete():
    nsi = _nsi()
    raw = to_raw_source_item(nsi, connector_key="gitlab")
    assert raw.source_type == "gitlab"
    assert raw.external_id == "x1"
    assert raw.external_version == "v1"
    assert raw.title == "t"
    assert raw.filename == "a.txt"
    assert raw.extension == "txt"
    assert raw.source_url == "https://x"
    assert raw.source_path == "dir/a.txt"
    assert raw.source_updated_at == "2026-01-01"
    assert raw.acl.scope == "project:1"
    assert raw.acl.is_resolved
    # raw 为空时不写 raw_acl（仅 Connector 提供 raw 才写）
    assert "raw_acl" not in raw.source_metadata
    assert raw.source_metadata["path"] == "dir/a.txt"


def test_payload_text():
    raw = to_raw_source_item(_nsi(content="hello", content_type="text"), connector_key="gitlab")
    assert raw.native_text == "hello"


def test_payload_bytes_for_pdf():
    nsi = _nsi(content="%PDF-1.4 fake", content_type="pdf", source_path="dir/a.pdf")
    raw = to_raw_source_item(nsi, connector_key="gitlab")
    assert raw.raw_bytes == b"%PDF-1.4 fake"


# ---------------------------------------------------------------------------
# ACL fail closed
# ---------------------------------------------------------------------------


def test_acl_missing_fail_closed():
    raw = to_raw_source_item(_nsi(acl_scope={}), connector_key="gitlab")
    assert raw.acl.is_fail_closed


def test_acl_scope_non_string_fail_closed():
    raw = to_raw_source_item(_nsi(acl_scope={"scope": 123}), connector_key="gitlab")
    assert raw.acl.is_fail_closed


def test_acl_groups_mapped():
    nsi = _nsi(acl_scope={"scope": "group:eng", "groups": ["eng", "ops"],
                          "resolve_failed": False, "raw": {}})
    raw = to_raw_source_item(nsi, connector_key="gitlab")
    assert raw.acl.scope == "group:eng"
    assert raw.acl.groups == frozenset({"eng", "ops"})
    assert raw.acl.is_resolved


def test_raw_acl_only_from_connector_no_forge():
    """raw_acl 不凭空生成：仅 Connector 实际提供 raw 时写。"""
    nsi = _nsi(acl_scope={"scope": "s1", "groups": [], "resolve_failed": False,
                          "raw": {"space_id": "s1"}})
    raw = to_raw_source_item(nsi, connector_key="dingtalk")
    assert raw.source_metadata["raw_acl"] == {"space_id": "s1"}


# ---------------------------------------------------------------------------
# 格式确定性判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext,kind", [
    ("md", ContentKind.MARKDOWN), ("markdown", ContentKind.MARKDOWN),
    ("py", ContentKind.CODE), ("js", ContentKind.CODE), ("json", ContentKind.CODE),
    ("csv", ContentKind.CSV), ("tsv", ContentKind.CSV),
    ("pdf", ContentKind.PDF), ("docx", ContentKind.OFFICE), ("xlsx", ContentKind.OFFICE),
    ("txt", ContentKind.TEXT), ("rst", ContentKind.TEXT),
])
def test_content_kind_from_ext(ext, kind):
    assert _content_kind_from_ext(ext) == kind


def test_content_kind_from_content_type_precedence():
    # content_type=md 优先
    raw = to_raw_source_item(_nsi(content_type="md", source_path="dir/a.md"), connector_key="gitlab")
    assert raw.content_kind == ContentKind.MARKDOWN
    # content_type=code
    raw2 = to_raw_source_item(_nsi(content_type="code", source_path="dir/a.py"), connector_key="gitlab")
    assert raw2.content_kind == ContentKind.CODE


def test_unknown_kind_goes_to_converter():
    raw = to_raw_source_item(_nsi(content_type="", source_path="dir/data.xyz"), connector_key="gitlab")
    assert raw.content_kind == ContentKind.UNKNOWN


# ---------------------------------------------------------------------------
# 端到端 convert_normalized
# ---------------------------------------------------------------------------


def test_convert_normalized_gitlab_markdown():
    nsi = _nsi(content_type="md", source_path="docs/a.md", content="# 标题\n\n正文")
    note = convert_normalized(nsi, connector_key="gitlab")
    assert note.conversion_status.value == "converted"
    assert note.body == "# 标题\n\n正文"
    assert note.source_type == "gitlab"
    assert note.source_id == "x1"
    assert note.source_hash and note.body_hash
    # 透传时 body 与原内容相同 → 两个 hash 相等是合法的；关键是不混淆语义。
    # 用非透传（如代码包裹）验证 source_hash（原内容）与 body_hash（包裹后）分离。
    import hashlib
    assert note.source_hash == hashlib.sha256("# 标题\n\n正文".encode("utf-8")).hexdigest()
    assert note.body_hash == hashlib.sha256(note.body.encode("utf-8")).hexdigest()


def test_convert_normalized_gitlab_code():
    nsi = _nsi(content_type="code", source_path="src/a.py", content="def f():\n    return 1")
    note = convert_normalized(nsi, connector_key="gitlab")
    assert note.body == "```python\ndef f():\n    return 1\n```"
    # source_hash = 原始内容 hash；body_hash = 包裹后 hash（两者不同，证明分离）
    import hashlib
    assert note.source_hash == hashlib.sha256("def f():\n    return 1".encode("utf-8")).hexdigest()
    assert note.body_hash == hashlib.sha256(note.body.encode("utf-8")).hexdigest()
    assert note.source_hash != note.body_hash


def test_convert_normalized_text_payload_garbled_failed():
    """文本载荷含乱码 → text converter 失败 → failed（非 blocked）。"""
    nsi = _nsi(content_type="", source_path="data.xyz", content="\x00\x01garbage")
    note = convert_normalized(nsi, connector_key="gitlab")
    assert note.conversion_status.value == "failed"


def test_convert_normalized_acl_preserved():
    nsi = _nsi(content_type="md", source_path="docs/a.md", content="# x",
               acl_scope={"scope": "project:1", "groups": [], "resolve_failed": False,
                          "raw": {"project_id": 1}})
    note = convert_normalized(nsi, connector_key="gitlab")
    assert note.acl.scope == "project:1"
    assert note.acl.is_resolved
    # raw_acl 只在 source_metadata
    assert note.source_metadata["raw_acl"] == {"project_id": 1}
    assert "raw" not in note.acl.to_dict()
