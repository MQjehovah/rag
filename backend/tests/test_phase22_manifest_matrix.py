"""Phase 2.2：状态驱动的 Manifest 载荷选择 + 严格历史 Markdown + 测试真实性。"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from app.sources.dingtalk import DingTalkConnector
from app.sources.schemas import SourcePayloadError
from app.core.dingtalk_storage import DingTalkLocalStorage


def _legacy_md(doc_id: str, *, converter: str = "hybrid_pdf") -> str:
    return (
        '---\n'
        f'source_type: "dingtalk"\n'
        f'dingtalk_node_id: "{doc_id}"\n'
        'conversion_pipeline_version: "dingtalk-markdown-pipeline-v24"\n'
        f'converter: "{converter}"\n'
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
# 状态矩阵
# ---------------------------------------------------------------------------


def test_converted_valid_markdown(tmp_path):
    md = _legacy_md("d1")
    raw = b"%PDF real raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    f = _md(storage, "d1", md)
    _raw(storage, "d1", raw)
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.metadata_json.get("manifest_is_markdown") is True
    assert "# 正文" in item.content
    assert item.input_representation.value == "preconverted_markdown"


def test_converted_markdown_missing_raw_exists_fail_closed(tmp_path):
    """converted + Markdown 缺失 + raw 存在 → 必须 fail closed，不得自动重新转换。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
    })
    _raw(storage, "d1", b"%PDF-1.4 raw exists")  # raw 存在
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "SOURCE_FILE_MISSING"


def test_converted_invalid_frontmatter_raw_exists_fail_closed(tmp_path):
    raw = b"%PDF raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    f = _md(storage, "d1", "没有 frontmatter 的普通 markdown")
    _raw(storage, "d1", raw)
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "LEGACY_MARKDOWN_UNRECOGNIZED"


def test_converted_markdown_hash_missing_fail_closed(tmp_path):
    """converted 但未声明 markdown_hash → fail closed（Phase 2.3 严格 converted）。"""
    md = _legacy_md("d1")
    raw = b"%PDF raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "converted",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
        # 无 markdown_hash
    })
    _md(storage, "d1", md)
    _raw(storage, "d1", raw)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "MANIFEST_STATE_CONFLICT"


def test_converted_markdown_hash_illegal_fail_closed(tmp_path):
    md = _legacy_md("d1")
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "", "markdown_hash": "not-a-hash", "conversion_status": "converted",
    })
    _md(storage, "d1", md)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "MARKDOWN_HASH_INVALID"


def test_converted_markdown_hash_mismatch_fail_closed(tmp_path):
    md = _legacy_md("d1")
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "", "markdown_hash": "0" * 64, "conversion_status": "converted",
    })
    _md(storage, "d1", md)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "MARKDOWN_HASH_MISMATCH"


def test_pending_stale_markdown_changed_raw(tmp_path):
    """pending + stale Markdown（非合法旧管道）+ 新 raw → 使用新 raw。"""
    raw = b"%PDF-1.4 new raw content"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "pending",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", "---\ntitle: 用户自定义\n---\n\n普通 Markdown")  # 非旧管道产物
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw  # 使用新 raw
    assert item.metadata_json.get("manifest_is_markdown") is not True


def test_failed_stale_markdown_uses_raw(tmp_path):
    raw = b"%PDF failed raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "conversion_status": "failed",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", _legacy_md("d1"))  # stale 但合法旧管道 → failed 仍强制 raw
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw


def test_legacy_no_status_valid_old_markdown(tmp_path):
    """legacy status=converted + 合法旧 Markdown → 判为已转换（走严格 converted）。"""
    md = _legacy_md("d1")
    raw = b"%PDF legacy raw"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "status": "converted",  # 旧字段 → inferred converted
        "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
        "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    f = _md(storage, "d1", md)
    _raw(storage, "d1", raw)
    manifest = storage.read_manifest()
    manifest["documents"][0]["markdown_hash"] = hashlib.sha256(f.read_bytes()).hexdigest()
    storage._write_manifest(manifest)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.metadata_json.get("manifest_is_markdown") is True


def test_legacy_no_status_user_frontmatter_uses_raw(tmp_path):
    """legacy 无 status + 普通用户 Frontmatter + raw → 使用 raw。"""
    raw = b"%PDF raw user frontmatter"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "markdown/d1.md",
        "raw_path": "raw/d1.pdf", "source_file_hash": hashlib.sha256(raw).hexdigest(),
    })
    _md(storage, "d1", "---\ntitle: 用户文档\nauthor: alice\n---\n\n正文")  # 普通 Frontmatter
    _raw(storage, "d1", raw)
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.content_bytes == raw
    assert item.metadata_json.get("manifest_is_markdown") is not True


def test_deleted_missing_files_ok(tmp_path):
    """deleted + 文件全部缺失 → 仍返回 deleted NormalizedSourceItem。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "deleted", "markdown_path": "markdown/d1.md", "raw_path": "raw/d1.pdf",
    })
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.deleted is True
    assert item.content == "" and item.content_bytes is None


def test_deleted_traversal_path_ok(tmp_path):
    """deleted + 历史越界路径 → 仍返回 deleted（不校验路径）。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "deleted", "markdown_path": "../../evil.md", "raw_path": "../../evil.pdf",
    })
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.deleted is True


def test_source_raw_hash_mismatch_fail_closed(tmp_path):
    raw = b"%PDF-1.4"
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "", "raw_path": "raw/d1.pdf",
        "source_file_hash": "0" * 64,  # 伪造
        "conversion_status": "pending",
    })
    _raw(storage, "d1", raw)
    with pytest.raises(SourcePayloadError) as excinfo:
        asyncio.run(_conn(storage).fetch_item("d1"))
    assert excinfo.value.error_code == "SOURCE_HASH_MISMATCH"


def test_deleted_no_loader_validation(tmp_path):
    """deleted 不调用 loader，不因历史路径异常中断。"""
    storage = _mk_storage(tmp_path, {
        "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
        "source_status": "deleted", "markdown_path": "missing.md", "raw_path": "missing.pdf",
        "source_file_hash": "not-a-hash",
    })
    item = asyncio.run(_conn(storage).fetch_item("d1"))
    assert item.deleted is True


# ---------------------------------------------------------------------------
# 严格历史 Markdown 识别（禁止任意字段判断）
# ---------------------------------------------------------------------------


def test_legacy_markdown_requires_all_four_conditions(tmp_path):
    """缺少任一条件（source_type/dingtalk_node_id/pipeline/converter）→ 不识别为旧管道。"""
    raw = b"%PDF-1.4"
    cases = [
        # 缺 source_type
        '---\ndingtalk_node_id: "d1"\nconversion_pipeline_version: "v24"\nconverter: "c"\n---\n\n正文',
        # node_id 不匹配
        '---\nsource_type: "dingtalk"\ndingtalk_node_id: "OTHER"\nconversion_pipeline_version: "v24"\nconverter: "c"\n---\n\n正文',
        # 缺 pipeline
        '---\nsource_type: "dingtalk"\ndingtalk_node_id: "d1"\nconverter: "c"\n---\n\n正文',
        # 缺 converter
        '---\nsource_type: "dingtalk"\ndingtalk_node_id: "d1"\nconversion_pipeline_version: "v24"\n---\n\n正文',
    ]
    for idx, md in enumerate(cases):
        storage = _mk_storage(tmp_path, {
            "document_id": "d1", "name": "d1", "extension": "pdf", "space_id": "sp1",
            "source_status": "active", "markdown_path": "markdown/d1.md",
            "raw_path": "raw/d1.pdf", "source_file_hash": hashlib.sha256(raw).hexdigest(),
        })
        _md(storage, "d1", md)
        _raw(storage, "d1", raw)
        item = asyncio.run(_conn(storage).fetch_item("d1"))
        # 不识别为旧管道 → 用 raw
        assert item.metadata_json.get("manifest_is_markdown") is not True, f"case {idx} 误判"


# ---------------------------------------------------------------------------
# 测试真实性：Fake PDF Converter 注入 + 调用次数
# ---------------------------------------------------------------------------


def test_dingtalk_real_raw_bytes_injected_converter(tmp_path, monkeypatch):
    """未转换 raw → 注入 Fake PDF Converter 收到真实 bytes，调用恰好一次，未调 Markdown。"""
    raw = b"%PDF-1.4 fake real bytes"
    storage = _mk_storage(tmp_path, {
        "document_id": "doc1", "name": "doc1", "extension": "pdf", "space_id": "sp1",
        "source_status": "active", "markdown_path": "",
        "raw_path": "raw/doc1.pdf", "source_file_hash": hashlib.sha256(raw).hexdigest(),
        "conversion_status": "downloaded",
    })
    _raw(storage, "doc1", raw)

    # 注入 Fake PDF Converter（替换 CanonicalNoteService registry 中的 pdf converter）
    from app.core.source_conversion import CanonicalNoteService, build_builtin_registry
    from app.core.source_conversion.base import ConverterMatch, EvidenceStrength
    from app.core.source_conversion.schemas import ContentKind, ConverterOutput
    from app.sources.conversion_adapter import convert_normalized

    calls = {"pdf": 0, "markdown": 0}
    registry = build_builtin_registry(seal=False)

    class _FakePdf:
        key = "pdf"
        version = "v1"
        priority = 5
        output_kind = ContentKind.PDF

        def match(self, item):
            # 只对 %PDF 文件头（真实语义）匹配
            if item.raw_bytes[:4] == b"%PDF":
                return ConverterMatch(self.key, EvidenceStrength.FILE_HEADER, reason="%PDF header")
            return None

        def convert(self, item, selected_match):
            calls["pdf"] += 1
            assert item.raw_bytes == raw  # 收到真实 raw bytes
            return ConverterOutput(body="PDF 正文", content_kind=ContentKind.PDF)

    class _FakeMarkdown:
        key = "markdown"
        version = "v1"
        priority = 10
        output_kind = ContentKind.MARKDOWN

        def match(self, item):
            # 只在 content_kind=markdown 时匹配
            if item.content_kind == ContentKind.MARKDOWN:
                return ConverterMatch(self.key, EvidenceStrength.CONTENT_KIND, reason="markdown")
            return None

        def convert(self, item, selected_match):
            calls["markdown"] += 1
            return ConverterOutput(body="# m", content_kind=ContentKind.MARKDOWN)

    registry.replace(_FakePdf())
    registry.replace(_FakeMarkdown())
    svc = CanonicalNoteService(registry)

    conn = _conn(storage)
    item = asyncio.run(conn.fetch_item("doc1"))
    assert item.content_bytes == raw
    note = convert_normalized(item, connector_key="dingtalk", service=svc)
    assert note.body == "PDF 正文"
    assert calls["pdf"] == 1  # 恰好一次
    assert calls["markdown"] == 0  # 未调 Markdown Converter
