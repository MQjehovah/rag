import json

import pytest

from app.config import settings
from app.core.dingtalk import DingTalkClient
from app.core.dingtalk_storage import DingTalkLocalStorage


def test_sync_scope_defaults_to_configured_workspace(monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_sync_scope", "configured")
    monkeypatch.setattr(settings, "dingtalk_knowledge_base_id", "space-001")

    assert DingTalkClient._target_workspace_id() == "space-001"
    assert DingTalkClient._target_workspace_id("space-requested") == "space-requested"


def test_sync_scope_requires_explicit_all_authorized(monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_sync_scope", "all_authorized")
    assert DingTalkClient._target_workspace_id() is None

    monkeypatch.setattr(settings, "dingtalk_sync_scope", "configured")
    monkeypatch.setattr(settings, "dingtalk_knowledge_base_id", "")
    with pytest.raises(ValueError, match="DINGTALK_KNOWLEDGE_BASE_ID"):
        DingTalkClient._target_workspace_id()


def test_supported_document_types_are_configurable(monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_supported_extensions", "pdf, docx, .md")
    monkeypatch.setattr(settings, "dingtalk_include_wiki", True)

    assert DingTalkClient.supported_extensions() == {"pdf", "docx", "md"}
    assert DingTalkClient._is_supported_document("pdf", "FILE") is True
    assert DingTalkClient._is_supported_document("xlsx", "FILE") is False
    assert DingTalkClient._is_supported_document("", "DOC") is True


def test_storage_initializes_expected_directory_structure(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    storage.ensure_directories()

    assert storage.raw_root.is_dir()
    assert storage.markdown_root.is_dir()
    assert storage.assets_root.is_dir()
    manifest = json.loads(storage.manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 3
    assert manifest["documents"] == []
    assert manifest["summary"]["document_count"] == 0
    assert manifest["created_at"]
    assert manifest["updated_at"]


def test_inventory_records_complete_document_metadata(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    storage.record_inventory([{
        "id": "node-001",
        "title": "部署手册.pdf",
        "extension": "pdf",
        "node_type": "FILE",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "设备资料/部署手册.pdf",
        "source_url": "https://example.test/node-001",
        "updated_at": "2026-08-04T10:00:00Z",
        "file_size": 2048,
    }])

    entry = storage.read_manifest()["documents"][0]
    assert entry["document_id"] == "node-001"
    assert entry["status"] == "discovered"
    assert entry["dingtalk_path"] == "设备资料/部署手册.pdf"
    assert entry["source_url"] == "https://example.test/node-001"
    assert entry["source_updated_at"] == "2026-08-04T10:00:00Z"
    assert entry["reported_file_size"] == 2048
    assert entry["raw_path"].startswith("raw/")


def test_persist_raw_file_is_atomic_and_detects_unchanged_content(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-001",
        "title": "部署手册.pdf",
        "extension": "pdf",
        "space_name": "产品知识库",
        "path": "设备资料/部署手册.pdf",
    }
    storage.record_inventory([document])

    first = storage.persist_raw_file(
        document, b"original-pdf", mime_type="application/pdf"
    )
    second = storage.persist_raw_file(
        document, b"original-pdf", mime_type="application/pdf"
    )

    raw_path = storage.root / first["raw_path"]
    assert raw_path.read_bytes() == b"original-pdf"
    assert first["raw_write_result"] == "written"
    assert second["raw_write_result"] == "unchanged"
    assert not list(raw_path.parent.glob("*.part"))
    entry = storage.read_manifest()["documents"][0]
    assert entry["status"] == "downloaded"
    assert entry["source_file_hash"] == first["source_file_hash"]
    assert entry["source_file_size"] == len(b"original-pdf")
    assert entry["download_status"] == "downloaded"
    assert entry["download_attempts"] == 2
    assert entry["conversion_status"] == "pending"
    assert any(event["stage"] == "download" for event in entry["status_history"])


def test_version_one_manifest_is_migrated_without_losing_data(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    storage.root.mkdir(parents=True)
    storage.manifest_path.write_text(
        json.dumps({
            "version": 1,
            "documents": [{
                "document_id": "node-old",
                "name": "旧文档.pdf",
                "status": "converted",
                "source_file_size": 10,
                "markdown_size": 20,
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    manifest = storage.read_manifest()

    entry = manifest["documents"][0]
    assert manifest["version"] == 3
    assert entry["document_id"] == "node-old"
    assert entry["download_status"] == "downloaded"
    assert entry["conversion_status"] == "converted"
    assert entry["rag_status"] == "pending"
    assert entry["pipeline_status"] == "converted"
    assert manifest["summary"]["markdown_bytes"] == 20
    assert entry["source_status"] == "active"


def test_unchanged_redownload_preserves_conversion_and_rag_status(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-stable",
        "title": "稳定文档.md",
        "extension": "md",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "稳定文档.md",
    }
    storage.record_inventory([document])
    storage.persist_raw_file(document, b"same-content")
    storage.persist_markdown_file(document, "# 稳定文档\n")
    entry = storage.read_manifest()["documents"][0]
    storage.update_rag_status(
        entry, "imported", rag_page_id="page-stable", rag_chunk_count=1
    )

    storage.persist_raw_file(document, b"same-content")

    entry = storage.read_manifest()["documents"][0]
    assert entry["status"] == "converted"
    assert entry["conversion_status"] == "converted"
    assert entry["rag_status"] == "imported"
    assert entry["source_versions"] == []


def test_same_content_retry_after_transient_download_failure_keeps_rag(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-transient",
        "title": "瞬时失败.md",
        "extension": "md",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "瞬时失败.md",
    }
    storage.record_inventory([document])
    storage.persist_raw_file(document, b"stable-source")
    storage.persist_markdown_file(document, "# 稳定正文\n")
    entry = storage.read_manifest()["documents"][0]
    storage.update_rag_status(
        entry,
        "imported",
        rag_page_id="page-transient",
        rag_chunk_count=1,
    )
    storage.update_document_status(document, "failed", error="网络瞬时超时")

    storage.persist_raw_file(document, b"stable-source")

    recovered = storage.read_manifest()["documents"][0]
    assert recovered["download_status"] == "downloaded"
    assert recovered["conversion_status"] == "converted"
    assert recovered["rag_status"] == "imported"
    assert recovered["rag_page_id"] == "page-transient"
    assert recovered["source_versions"] == []


def test_changed_source_invalidates_conversion_and_rag_but_keeps_version(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-change",
        "title": "更新文档.md",
        "extension": "md",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "更新文档.md",
    }
    storage.record_inventory([document])
    first = storage.persist_raw_file(document, b"version-one")
    storage.persist_markdown_file(document, "# 第一版\n")
    entry = storage.read_manifest()["documents"][0]
    storage.update_rag_status(
        entry, "imported", rag_page_id="page-change", rag_chunk_count=1
    )

    storage.persist_raw_file(document, b"version-two")

    entry = storage.read_manifest()["documents"][0]
    assert entry["pipeline_status"] == "source_changed"
    assert entry["conversion_status"] == "pending"
    assert entry["rag_status"] == "stale"
    assert entry["source_versions"][-1]["source_file_hash"] == first["source_file_hash"]
    assert entry["source_versions"][-1]["rag_page_id"] == "page-change"


def test_inventory_move_relocates_local_files_for_same_document_id(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    original = {
        "id": "node-move",
        "title": "手册.md",
        "extension": "md",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "旧目录/手册.md",
    }
    storage.record_inventory([original])
    storage.persist_raw_file(original, b"manual")
    storage.persist_markdown_file(original, "# 手册\n")
    before = storage.read_manifest()["documents"][0]
    old_raw = storage.root / before["raw_path"]
    old_markdown = storage.root / before["markdown_path"]
    moved = dict(original, path="新目录/手册.md")

    result = storage.record_inventory([moved])

    entry = storage.read_manifest()["documents"][0]
    assert result["moved"] == 1
    assert not old_raw.exists()
    assert not old_markdown.exists()
    assert (storage.root / entry["raw_path"]).read_bytes() == b"manual"
    assert (storage.root / entry["markdown_path"]).is_file()
    assert entry["path_history"][-1]["from"]["dingtalk_path"] == "旧目录/手册.md"


def test_only_complete_scoped_snapshot_soft_deletes_and_can_restore(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-delete",
        "title": "待删除.md",
        "extension": "md",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "待删除.md",
    }
    storage.record_inventory([document])
    storage.persist_raw_file(document, b"retained")
    storage.persist_markdown_file(document, "# 待删除\n")
    entry = storage.read_manifest()["documents"][0]
    storage.update_rag_status(
        entry, "imported", rag_page_id="page-delete", rag_chunk_count=1
    )

    storage.record_inventory([])
    assert storage.read_manifest()["documents"][0]["source_status"] == "active"

    result = storage.record_inventory(
        [], complete_snapshot=True, scope_space_ids=["space-001"]
    )
    deleted = storage.read_manifest()["documents"][0]
    assert result["deleted"] == 1
    assert deleted["source_status"] == "deleted"
    assert deleted["rag_status"] == "pending_delete"
    assert (storage.root / deleted["raw_path"]).is_file()

    result = storage.record_inventory(
        [document], complete_snapshot=True, scope_space_ids=["space-001"]
    )
    restored = storage.read_manifest()["documents"][0]
    assert result["restored"] == 1
    assert restored["source_status"] == "active"
    assert restored["rag_status"] == "imported"
    assert restored["restored_at"]


def test_complete_snapshot_requires_explicit_scope(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    with pytest.raises(ValueError, match="明确提供知识库范围"):
        storage.record_inventory([], complete_snapshot=True)


def test_sync_filter_change_does_not_create_false_deletion(monkeypatch, tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-filtered",
        "title": "旧类型.md",
        "extension": "md",
        "node_type": "FILE",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "旧类型.md",
    }
    storage.record_inventory([document])
    monkeypatch.setattr(settings, "dingtalk_supported_extensions", "pdf")
    monkeypatch.setattr(settings, "dingtalk_include_wiki", False)

    result = storage.record_inventory(
        [], complete_snapshot=True, scope_space_ids=["space-001"]
    )

    assert result["deleted"] == 0
    assert storage.read_manifest()["documents"][0]["source_status"] == "active"


def test_manifest_audit_verifies_files_and_rag_metadata(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-audit",
        "title": "审计文档.txt",
        "extension": "txt",
        "space_name": "产品知识库",
        "path": "审计/审计文档.txt",
    }
    storage.record_inventory([document])
    storage.persist_raw_file(document, b"source")
    storage.persist_markdown_file(document, "# 审计文档\n\n正文\n")
    entry = storage.read_manifest()["documents"][0]
    storage.update_rag_status(
        entry,
        "imported",
        rag_page_id="page-001",
        rag_chunk_count=2,
    )

    audit = storage.audit_manifest(record=True)

    assert audit["valid"] is True
    assert audit["error_count"] == 0
    manifest = storage.read_manifest()
    assert manifest["summary"]["pipeline_status_counts"] == {"imported": 1}
    assert manifest["last_audit"]["valid"] is True

    markdown_path = storage.root / entry["markdown_path"]
    markdown_path.write_text("被修改", encoding="utf-8")
    broken = storage.audit_manifest()
    assert broken["valid"] is False
    assert any("Markdown" in item["message"] for item in broken["errors"])


def test_streamed_temporary_file_is_verified_and_committed(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    document = {
        "id": "node-stream",
        "title": "大文件.pdf",
        "extension": "pdf",
        "space_name": "产品知识库",
        "path": "大文件/大文件.pdf",
    }
    temporary = storage.raw_temporary_path(document, "pdf")
    temporary.write_bytes(b"streamed-content")
    expected_hash = storage.file_sha256(temporary)

    metadata = storage.commit_raw_temporary_file(
        document,
        temporary,
        extension="pdf",
        mime_type="application/pdf",
        expected_hash=expected_hash,
        expected_size=len(b"streamed-content"),
    )

    assert not temporary.exists()
    assert (storage.root / metadata["raw_path"]).read_bytes() == b"streamed-content"
    assert metadata["source_file_hash"] == expected_hash


def test_document_paths_preserve_hierarchy_and_avoid_name_collisions(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    first = storage.document_paths({
        "id": "node-001",
        "space_name": "产品知识库",
        "path": "设备资料/部署手册/Titan810.pdf",
        "title": "Titan810.pdf",
        "extension": "pdf",
    })
    second = storage.document_paths({
        "id": "node-002",
        "space_name": "产品知识库",
        "path": "设备资料/部署手册/Titan810.pdf",
        "title": "Titan810.pdf",
        "extension": "pdf",
    })

    assert first.raw_path.parent.parts[-3:] == (
        "产品知识库", "设备资料", "部署手册",
    )
    assert first.raw_path.suffix == ".pdf"
    assert first.markdown_path.suffix == ".md"
    assert first.raw_path != second.raw_path
    assert first.markdown_path != second.markdown_path


def test_document_paths_sanitize_unsafe_names_and_block_traversal(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    paths = storage.document_paths({
        "id": "node-unsafe",
        "space_name": "CON",
        "path": "../../项目:资料/报告?.pdf",
        "title": "报告?.pdf",
        "extension": "pdf",
    })

    paths.raw_path.relative_to(storage.raw_root)
    paths.markdown_path.relative_to(storage.markdown_root)
    assert ".." not in paths.raw_path.parts
    assert "_CON" in paths.raw_path.parts
    assert ":" not in paths.raw_path.name
    assert "?" not in paths.raw_path.name


@pytest.mark.asyncio
async def test_recursive_inventory_preserves_workspace_and_folder_metadata(monkeypatch):
    monkeypatch.setattr(settings, "dingtalk_include_subfolders", True)
    monkeypatch.setattr(settings, "dingtalk_supported_extensions", "pdf")
    client = object.__new__(DingTalkClient)
    client._on_progress = None
    client._collected_count = 0

    async def fake_list_nodes(parent_id):
        if parent_id == "root":
            return [{
                "node_id": "folder-1",
                "name": "设备资料",
                "type": "FOLDER",
                "extension": "",
                "has_children": True,
            }]
        return [{
            "node_id": "node-001",
            "name": "部署手册.pdf",
            "type": "FILE",
            "extension": "pdf",
            "has_children": False,
            "updated_at": "2026-08-04T10:00:00Z",
            "file_size": 2048,
            "url": "https://example.test/node-001",
        }]

    client.list_nodes = fake_list_nodes
    docs = await client._list_recursive(
        "space-001", "产品知识库", "root"
    )

    assert docs == [{
        "id": "node-001",
        "title": "部署手册.pdf",
        "extension": "pdf",
        "node_type": "FILE",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "设备资料/部署手册.pdf",
        "source_url": "https://example.test/node-001",
        "updated_at": "2026-08-04T10:00:00Z",
        "file_size": 2048,
    }]


@pytest.mark.asyncio
async def test_download_selected_raw_files_does_not_convert_content(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "dingtalk_local_storage_dir", str(tmp_path / "dingtalk"))
    monkeypatch.setattr(settings, "dingtalk_supported_extensions", "pdf")
    client = object.__new__(DingTalkClient)

    async def fake_raw_download(document, extension, storage):
        metadata = storage.persist_raw_file(
            document,
            b"%PDF-raw-only",
            extension=extension,
            mime_type="application/pdf",
        )
        metadata["source_extension"] = extension
        return metadata

    client.download_node_raw_to_local = fake_raw_download
    downloaded = await client.download_selected_raw_files([{
        "id": "node-raw",
        "title": "原始手册.pdf",
        "extension": "pdf",
        "node_type": "FILE",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "手册/原始手册.pdf",
    }])

    assert len(downloaded) == 1
    assert "content" not in downloaded[0]
    assert "source_bytes" not in downloaded[0]
    assert downloaded[0]["source_file_size"] == len(b"%PDF-raw-only")
    raw_path = tmp_path / "dingtalk" / downloaded[0]["raw_path"]
    assert raw_path.read_bytes() == b"%PDF-raw-only"
