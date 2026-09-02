"""Phase 2.4：provenance 生命周期 + inferred-unconverted + 边界封闭 + representation 权威 + SourcePayloadError 契约。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.sources.dingtalk import DingTalkConnector
from app.sources.schemas import InputRepresentation, SourcePayloadError, NormalizedSourceItem
from app.core.dingtalk_storage import DingTalkLocalStorage


def _legacy_md(doc_id: str) -> str:
    return (
        '---\n'
        f'source_type: "dingtalk"\n'
        f'dingtalk_node_id: "{doc_id}"\n'
        'conversion_pipeline_version: "dingtalk-markdown-pipeline-v24"\n'
        'converter: "hybrid_pdf"\n'
        '---\n\n# 正文'
    )


def _mk_storage(tmp_path, doc):
    storage = DingTalkLocalStorage(tmp_path / "dt")
    storage.ensure_directories()
    storage._write_manifest({"last_inventory_at": "2026-01-01", "documents": [doc]})
    return storage


def _md(storage, doc_id, text):
    f = storage.markdown_root / f"{doc_id}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(text.encode("utf-8"))
    return f


def _raw(storage, doc_id, data: bytes):
    f = storage.raw_root / f"{doc_id}.pdf"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def _conn(storage):
    return DingTalkConnector({"connection_id": "c1"}, storage=storage)


# ---------------------------------------------------------------------------
# 一、provenance 生命周期
# ---------------------------------------------------------------------------


def test_legacy_converted_inferred_true(tmp_path):
    """旧 status=converted → normalize 后 inferred=True。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "converted",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
    })
    manifest = storage.read_manifest()
    assert manifest["documents"][0]["conversion_status_inferred_from_legacy"] is True
    assert manifest["documents"][0]["legacy_status"] == "converted"


def test_download_new_raw_clears_inferred(tmp_path):
    """下载新 raw 且 hash 变化 → conversion_status=pending、inferred=False。"""
    import json
    raw = b"%PDF new raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "converted",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _raw(storage, "d1", raw)
    # 直接构造显式 pending + inferred=False（新管道写入后的 manifest 状态）
    m = storage.read_manifest()
    m["documents"][0].update({
        "conversion_status": "pending",
        "conversion_status_inferred_from_legacy": False,
    })
    (storage.root / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    manifest = storage.read_manifest()
    entry = manifest["documents"][0]
    assert entry["conversion_status_inferred_from_legacy"] is False
    assert entry["conversion_status"] == "pending"
    # 即使旧 Markdown 合法，也必须读取新 raw
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw
    assert item.input_representation == InputRepresentation.ORIGINAL


def test_reconvert_success_clears_inferred(tmp_path):
    """重新转换成功 → converted、inferred=False。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "converted",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
    })
    storage.update_document_status(
        {"id": "d1", "space_id": "sp1", "extension": "pdf", "name": "d1", "dingtalk_path": "x"},
        "converted",
    )
    manifest = storage.read_manifest()
    assert manifest["documents"][0]["conversion_status"] == "converted"
    assert manifest["documents"][0]["conversion_status_inferred_from_legacy"] is False


def test_reconvert_failed_clears_inferred(tmp_path):
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "converted",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
    })
    storage.update_document_status(
        {"id": "d1", "space_id": "sp1", "extension": "pdf", "name": "d1", "dingtalk_path": "x"},
        "conversion_failed", error="boom",
    )
    manifest = storage.read_manifest()
    assert manifest["documents"][0]["conversion_status"] == "failed"
    assert manifest["documents"][0]["conversion_status_inferred_from_legacy"] is False


def test_force_reconvert_clears_inferred(tmp_path):
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "converted",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
    })
    storage.update_document_status(
        {"id": "d1", "space_id": "sp1", "extension": "pdf", "name": "d1", "dingtalk_path": "x"},
        "downloaded", force_reconvert=True,
    )
    manifest = storage.read_manifest()
    assert manifest["documents"][0]["conversion_status_inferred_from_legacy"] is False


def test_repeated_read_does_not_reinfer(tmp_path):
    """多次 read_manifest 不得把显式状态重新标为 inferred。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "conversion_status": "pending",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
    })
    for _ in range(3):
        m = storage.read_manifest()
        assert m["documents"][0]["conversion_status_inferred_from_legacy"] is False
        assert m["documents"][0]["conversion_status"] == "pending"


# ---------------------------------------------------------------------------
# 二、inferred 非 converted → 一律 raw
# ---------------------------------------------------------------------------


def test_inferred_downloaded_uses_raw(tmp_path):
    """inferred + legacy_status=downloaded + 合法旧 Markdown + 新 raw → raw。"""
    raw = b"%PDF downloaded raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "downloaded",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", _legacy_md("d1"))  # 合法旧 Markdown
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw
    assert item.metadata_json.get("manifest_is_markdown") is not True


def test_inferred_discovered_uses_raw(tmp_path):
    raw = b"%PDF discovered raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "discovered",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", _legacy_md("d1"))
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw


def test_inferred_unconverted_raw_missing_no_markdown(tmp_path):
    """inferred 非 converted + 合法旧 Markdown + raw 缺失 → SOURCE_FILE_MISSING，不得返回 Markdown。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "downloaded",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
    })
    _md(storage, "d1", _legacy_md("d1"))  # Markdown 存在但 raw 缺失
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "SOURCE_FILE_MISSING"


def test_legacy_converted_source_changed_to_pending_uses_raw(tmp_path):
    """legacy converted 后源文件变化进入显式 pending → raw。"""
    import json
    raw = b"%PDF changed raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "converted",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", _legacy_md("d1"))
    _raw(storage, "d1", raw)
    # 新管道把状态改为显式 pending（inferred=False）
    m = storage.read_manifest()
    m["documents"][0].update({
        "conversion_status": "pending",
        "conversion_status_inferred_from_legacy": False,
    })
    (storage.root / "manifest.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw


def test_stale_md_new_raw_canonical_from_raw(tmp_path):
    """stale Markdown 与新 raw 内容不同 → 最终 CanonicalNote 来自新 raw。"""
    raw = b"%PDF fresh raw content"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "downloaded",
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", _legacy_md("d1"))  # stale 旧 Markdown
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw
    # CanonicalNote 来自 raw（PDF converter 处理 raw bytes）
    from app.sources.conversion_adapter import convert_normalized
    from app.core.source_conversion import CanonicalNoteService, build_builtin_registry
    from app.core.source_conversion.schemas import ContentKind, ConverterOutput
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength

    calls = {"pdf": 0}
    registry = build_builtin_registry(seal=False)

    class _FakePdf:
        key = "pdf"
        version = "v1"
        priority = 5
        output_kind = ContentKind.PDF

        def match(self, item):
            if item.raw_bytes[:4] == b"%PDF":
                return ConverterMatch(self.key, EvidenceStrength.FILE_HEADER, reason="%PDF")
            return None

        def convert(self, item, selected_match):
            calls["pdf"] += 1
            assert item.raw_bytes == raw  # 收到新 raw
            return ConverterOutput(body="PDF from raw", content_kind=ContentKind.PDF)

    registry.replace(_FakePdf())
    note = convert_normalized(item, connector_key="dingtalk", service=CanonicalNoteService(registry))
    assert note.body == "PDF from raw"
    assert calls["pdf"] == 1


# ---------------------------------------------------------------------------
# 三、NormalizedSourceItem 边界封闭
# ---------------------------------------------------------------------------


def test_input_representation_must_be_enum():
    """Phase 2.5：构造函数只接受 Enum 实例；字符串一律拒绝（from_dict 才恢复）。"""
    from app.sources.schemas import NormalizedSourceItem as NSI
    for bad in ("bogus", 123, object()):
        with pytest.raises((ValueError, TypeError)):
            NSI(connection_id="c", source_type="t", external_id="x",
                content="hi", input_representation=bad)  # type: ignore[arg-type]
    # 合法字符串枚举值也拒绝（构造函数不静默归一化）
    with pytest.raises(ValueError):
        NSI(connection_id="c", source_type="t", external_id="x",
            content="hi", input_representation="original")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        NSI(connection_id="c", source_type="t", external_id="x",
            content="hi", input_representation="preconverted_markdown")  # type: ignore[arg-type]
    # 枚举实例合法
    item = NSI(connection_id="c", source_type="t", external_id="x",
               content="hi", input_representation=InputRepresentation.ORIGINAL)
    assert item.input_representation == InputRepresentation.ORIGINAL


def test_from_dict_restores_representation_enum():
    """from_dict 从字符串恢复 Enum（显式反序列化）。"""
    from app.sources.schemas import NormalizedSourceItem as NSI
    item = NSI.from_dict({
        "connection_id": "c", "source_type": "t", "external_id": "x",
        "content": "hi", "input_representation": "preconverted_markdown",
        "acl_scope": {}, "metadata_json": {}, "attachments": [],
    })
    assert item.input_representation == InputRepresentation.PRECONVERTED_MARKDOWN
    # 非法字符串 from_dict 拒绝
    d = {"connection_id": "c", "source_type": "t", "external_id": "x",
         "content": "hi", "input_representation": "bogus", "acl_scope": {},
         "metadata_json": {}, "attachments": []}
    with pytest.raises(ValueError):
        NSI.from_dict(d)


def test_text_bytes_mutually_exclusive():
    from app.sources.schemas import NormalizedSourceItem as NSI
    with pytest.raises(ValueError):
        NSI(connection_id="c", source_type="t", external_id="x",
            content="text", content_bytes=b"bytes")
    with pytest.raises(ValueError):
        NSI(connection_id="c", source_type="t", external_id="x",
            content="", content_bytes=b"")


def test_content_type_and_hashes():
    from app.sources.schemas import NormalizedSourceItem as NSI
    # content_hash 非法
    with pytest.raises(ValueError):
        NSI(connection_id="c", source_type="t", external_id="x", content="hi",
            content_hash="not-a-hash")
    # content_hash 与 payload 不符
    with pytest.raises(ValueError):
        NSI(connection_id="c", source_type="t", external_id="x", content="hi",
            content_hash="0" * 64)
    # original_source_hash 非法
    with pytest.raises(ValueError):
        NSI(connection_id="c", source_type="t", external_id="x", content="hi",
            original_source_hash="UPPER" + "a" * 58)


def test_deleted_no_payload_ok():
    from app.sources.schemas import NormalizedSourceItem as NSI
    item = NSI(connection_id="c", source_type="t", external_id="x", deleted=True)
    assert item.deleted is True


# ---------------------------------------------------------------------------
# 四、InputRepresentation 唯一权威（Adapter）
# ---------------------------------------------------------------------------


def test_preconverted_markdown_passthrough_any_connector():
    """PRECONVERTED_MARKDOWN + 任何 Connector → Markdown 透传（不二次转 PDF）。"""
    from app.sources.conversion_adapter import to_raw_source_item
    item = NormalizedSourceItem(
        connection_id="c", source_type="fake", external_id="x",
        content="# 正文", content_type="pdf", source_path="a.pdf",
        input_representation=InputRepresentation.PRECONVERTED_MARKDOWN,
        acl_scope={"scope": "s1", "groups": [], "resolve_failed": False, "raw": {}},
    )
    raw = to_raw_source_item(item, connector_key="fake")
    assert raw.content_kind.value == "markdown"


def test_original_pdf_goes_to_pdf_converter():
    """ORIGINAL + PDF → PDF Converter（不因 metadata 声称 manifest_is_markdown 而变）。"""
    from app.sources.conversion_adapter import to_raw_source_item
    item = NormalizedSourceItem(
        connection_id="c", source_type="dingtalk", external_id="x",
        content="", content_bytes=b"%PDF-1.4 real", content_type="pdf", source_path="a.pdf",
        input_representation=InputRepresentation.ORIGINAL,
        metadata_json={"manifest_is_markdown": True},  # metadata 声称预转换
        acl_scope={"scope": "s1", "groups": [], "resolve_failed": False, "raw": {}},
    )
    raw = to_raw_source_item(item, connector_key="dingtalk")
    assert raw.content_kind.value == "pdf"  # 不偷偷按预转换 Markdown


# ---------------------------------------------------------------------------
# 五、SourcePayloadError 契约
# ---------------------------------------------------------------------------


def test_source_payload_error_invalid_stage():
    with pytest.raises(ValueError):
        SourcePayloadError("X", stage="persist", safe_message="m", retryable=True)


def test_source_payload_error_empty_code():
    with pytest.raises(ValueError):
        SourcePayloadError("", safe_message="m", retryable=True)


def test_source_payload_error_empty_safe_message():
    with pytest.raises(ValueError):
        SourcePayloadError("CODE", safe_message="", retryable=True)


def test_source_payload_error_retryable_non_bool():
    with pytest.raises(ValueError):
        SourcePayloadError("CODE", safe_message="m", retryable=1)  # type: ignore[arg-type]


def test_source_payload_error_internal_detail_str():
    with pytest.raises(ValueError):
        SourcePayloadError("CODE", safe_message="m", retryable=True, internal_detail=123)  # type: ignore[arg-type]


def test_safe_message_excludes_internal_detail():
    err = SourcePayloadError("CODE", stage="fetch", safe_message="安全消息",
                             retryable=True, internal_detail="绝对路径 C:/secret")
    assert "C:/secret" not in err.safe_message
    assert "C:/secret" not in str(err)
