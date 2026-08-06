import hashlib
import json
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.core.dingtalk_converter import CONVERSION_PIPELINE_VERSION
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.remote_rag import (
    DingTalkRemoteRAGImporter,
    RemoteRAGClient,
    RemoteRAGQualityGate,
    RemoteRAGSyncState,
)


def prepare_latest_entry(storage: DingTalkLocalStorage, markdown: str, node_id="node-1"):
    document = {
        "id": node_id,
        "title": "收费标准.pdf",
        "extension": "pdf",
        "node_type": "FILE",
        "space_id": "space-1",
        "space_name": "交付服务部知识库",
        "path": "政策/收费标准.pdf",
    }
    storage.record_inventory([document])
    storage.persist_raw_file(document, b"%PDF-test")
    storage.persist_markdown_file(
        document,
        markdown,
        conversion_metadata={
            "converter": "hybrid_pdf",
            "conversion_pipeline_version": CONVERSION_PIPELINE_VERSION,
        },
    )
    return storage.read_manifest()["documents"][0]


def latest_markdown(body: str) -> str:
    return (
        "---\n"
        f'conversion_pipeline_version: "{CONVERSION_PIPELINE_VERSION}"\n'
        "---\n\n"
        "# 收费标准.pdf\n\n"
        f"{body}\n"
    )


def test_quality_gate_accepts_latest_markdown_and_existing_image(tmp_path, monkeypatch):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    image_root = tmp_path / "pdf-pages"
    image_hash = "a" * 64
    image_path = image_root / image_hash / "page-1-image-2.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"jpeg-image")
    monkeypatch.setattr(settings, "pdf_image_storage_dir", str(image_root))
    markdown_url = f"/api/upload/pdf-pages/{image_hash}/page-1-image-2.jpg"
    entry = prepare_latest_entry(
        storage,
        latest_markdown(
            "这是已经通过最新版转换链路生成的收费标准正文，内容完整。\n\n"
            f"![收费表]({markdown_url})"
        ),
    )

    prepared = RemoteRAGQualityGate(storage).validate_entry(entry)

    assert prepared.document_id == "node-1"
    assert prepared.images[0].path == image_path
    assert prepared.images[0].sha256 == hashlib.sha256(b"jpeg-image").hexdigest()


def test_preflight_plan_contains_hashes_and_no_credentials(tmp_path, monkeypatch):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    prepare_latest_entry(
        storage,
        latest_markdown("这是用于生成最终远程导入候选清单的完整正文内容。"),
    )
    importer = DingTalkRemoteRAGImporter(
        storage,
        FakeRemoteClient(),
        RemoteRAGSyncState(tmp_path / "remote-state.json"),
    )

    result = importer.preflight()
    path = importer.write_preflight_plan(result)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["passed"] == 1
    assert payload["target"]["notebook_name"] == "钉钉知识库"
    assert payload["documents"][0]["markdown_hash"]
    assert "password" not in path.read_text(encoding="utf-8").lower()


def test_remote_sync_state_retries_temporary_windows_file_lock(tmp_path, monkeypatch):
    state = RemoteRAGSyncState(tmp_path / "remote-state.json")
    original_replace = Path.replace
    attempts = {"count": 0}

    def flaky_replace(path, target):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError("模拟Windows临时文件占用")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    state.update_document("node-1", status="imported")

    assert attempts["count"] == 3
    assert RemoteRAGSyncState(state.path).document("node-1")["status"] == "imported"


def test_quality_gate_rejects_old_pipeline(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_latest_entry(
        storage,
        "---\nconversion_pipeline_version: \"old\"\n---\n\n# 标题\n\n存在�的正文内容",
    )

    with pytest.raises(ValueError, match="不是最新转换版本"):
        RemoteRAGQualityGate(storage).validate_entry(entry)


def test_quality_gate_rejects_replacement_character_in_latest_markdown(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_latest_entry(
        storage,
        latest_markdown("这是最新版转换结果，但正文中仍然含有�乱码，因此不允许远程导入。"),
    )

    with pytest.raises(ValueError, match="替换字符"):
        RemoteRAGQualityGate(storage).validate_entry(entry)


def test_quality_gate_rejects_invalid_table_merge_metadata(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_latest_entry(
        storage,
        latest_markdown(
            "这是用于检查表格合并信息的完整正文内容。\n\n"
            '<!-- rag-table-merges: {"version":1,"cells":[{"row":0,"col":0,'
            '"rowspan":0,"colspan":2}]} -->'
        ),
    )

    with pytest.raises(ValueError, match="超出安全范围"):
        RemoteRAGQualityGate(storage).validate_entry(entry)


def test_quality_gate_allows_adjacent_tables_with_different_widths(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_latest_entry(
        storage,
        latest_markdown(
            "这是包含两张相邻表格的完整正文内容。\n\n"
            "| 姓名 | 部门 | 日期 |\n"
            "| --- | --- | --- |\n"
            "| 张三 | 服务部 | 2026-08-05 |\n"
            "| 项目 | 结果 |\n"
            "| --- | --- |\n"
            "| 培训 | 通过 |"
        ),
    )

    prepared = RemoteRAGQualityGate(storage).validate_entry(entry)

    assert prepared.document_id == "node-1"


def test_quality_gate_rejects_inconsistent_rows_inside_one_table(tmp_path):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    entry = prepare_latest_entry(
        storage,
        latest_markdown(
            "这是用于检查表格列数错误的完整正文内容。\n\n"
            "| 姓名 | 部门 | 日期 |\n"
            "| --- | --- | --- |\n"
            "| 张三 | 服务部 |"
        ),
    )

    with pytest.raises(ValueError, match="列数不一致"):
        RemoteRAGQualityGate(storage).validate_entry(entry)


def test_quality_gate_rejects_missing_local_pdf_image(tmp_path, monkeypatch):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    monkeypatch.setattr(settings, "pdf_image_storage_dir", str(tmp_path / "pdf-pages"))
    entry = prepare_latest_entry(
        storage,
        latest_markdown(
            "正文内容足够长，用于验证缺失图片会阻断远程导入。\n\n"
            f"![图](/api/upload/pdf-pages/{'b' * 64}/page-1.jpg)"
        ),
    )

    with pytest.raises(ValueError, match="图片不存在"):
        RemoteRAGQualityGate(storage).validate_entry(entry)


@pytest.mark.asyncio
async def test_remote_client_requires_unique_exact_notebook(monkeypatch):
    monkeypatch.setattr(settings, "remote_rag_max_retries", 1)

    def handler(request: httpx.Request):
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"token": "token"})
        if request.url.path == "/api/notebooks":
            return httpx.Response(200, json=[
                {"id": "1", "name": "钉钉知识库"},
                {"id": "2", "name": "钉钉知识库"},
            ])
        return httpx.Response(404)

    client = RemoteRAGClient(
        "http://example.test",
        "admin",
        "password",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="数量为2"):
            await client.resolve_notebook("钉钉知识库")
    finally:
        await client.close()


class FakeRemoteClient:
    base_url = "http://teacher.test"

    def __init__(self):
        self.pages = {}
        self.uploads = 0
        self.creates = 0
        self.updates = 0
        self.indexes = 0

    async def resolve_notebook(self, name, notebook_id=""):
        assert name == "钉钉知识库"
        return {"id": notebook_id or "remote-notebook", "name": name}

    async def list_pages(self, _notebook_id):
        return [
            {"id": page["id"], "title": page["title"], "notebook_id": page["notebook_id"]}
            for page in self.pages.values()
        ]

    async def get_page(self, page_id):
        value = self.pages.get(page_id)
        return dict(value) if value else None

    async def upload_image(self, asset):
        self.uploads += 1
        return {"url": f"/api/upload/images/remote/{asset.sha256}.jpg", "name": asset.sha256}

    async def verify_asset(self, _url):
        return None

    async def create_page(self, title, content, notebook_id):
        self.creates += 1
        page_id = f"page-{self.creates}"
        page = {
            "id": page_id,
            "title": title,
            "content": content,
            "notebook_id": notebook_id,
            "index_status": "missing",
        }
        self.pages[page_id] = page
        return dict(page)

    async def update_page(self, page_id, title, content, notebook_id):
        self.updates += 1
        self.pages[page_id].update({
            "title": title,
            "content": content,
            "notebook_id": notebook_id,
            "index_status": "missing",
        })
        return dict(self.pages[page_id])

    async def import_source_page(
        self,
        document,
        content,
        content_hash,
        notebook_id,
        page_id="",
    ):
        if page_id:
            self.updates += 1
        else:
            self.creates += 1
            page_id = f"page-{self.creates}"
        page = self.pages.setdefault(page_id, {"id": page_id})
        page.update({
            "title": document.title,
            "content": content,
            "notebook_id": notebook_id,
            "source_type": "dingtalk",
            "source_id": document.document_id,
            "source_pipeline_version": document.pipeline_version,
            "source_markdown_hash": document.markdown_hash,
            "content_hash": content_hash,
            "index_status": "missing",
        })
        return dict(page)

    async def index_page(self, page_id):
        self.indexes += 1
        self.pages[page_id]["index_status"] = "current"
        return {"message": "索引成功，共1个分块"}

    async def search(self, query, top_k=5):
        results = [
            {
                "id": page["id"],
                "title": page["title"],
                "score": 1.0,
                "source": "keyword",
                "page_number": None,
            }
            for page in self.pages.values()
            if query in page["title"] or query in page.get("content", "")
        ][:top_k]
        return {"results": results, "total": len(results), "graph_expanded": 0}

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_remote_import_is_idempotent_and_rewrites_images(tmp_path, monkeypatch):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    image_root = tmp_path / "pdf-pages"
    image_hash = "c" * 64
    image_path = image_root / image_hash / "page-1.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"page-image")
    monkeypatch.setattr(settings, "pdf_image_storage_dir", str(image_root))
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_backup_dir", str(tmp_path / "backups"))
    prepare_latest_entry(
        storage,
        latest_markdown(
            "这是用于远程幂等导入测试的完整正文内容。\n\n"
            f"![页面图](/api/upload/pdf-pages/{image_hash}/page-1.jpg)"
        ),
    )
    client = FakeRemoteClient()
    state = RemoteRAGSyncState(tmp_path / "remote-state.json")
    importer = DingTalkRemoteRAGImporter(storage, client, state)

    first = await importer.import_manifest(snapshot=True)
    second = await importer.import_manifest(snapshot=False)

    assert first["imported"] == 1
    assert second["skipped"] == 1
    assert client.creates == 1
    assert client.uploads == 1
    assert client.indexes == 1
    page = client.pages["page-1"]
    assert "/api/upload/images/remote/" in page["content"]
    assert "/api/upload/pdf-pages/" not in page["content"]
    assert state.document("node-1")["remote_page_id"] == "page-1"
    assert first["snapshot_path"]


@pytest.mark.asyncio
async def test_remote_integrity_audit_and_repair_binary_drift(tmp_path, monkeypatch):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_backup_dir", str(tmp_path / "backups"))
    entry = prepare_latest_entry(
        storage,
        latest_markdown("这是通过最新版管线生成、需要用于恢复远端污染内容的正文。"),
    )
    expected_content = RemoteRAGQualityGate(storage).validate_entry(entry).content
    expected_hash = hashlib.sha256(expected_content.encode("utf-8")).hexdigest()
    client = FakeRemoteClient()
    client.pages["page-corrupted"] = {
        "id": "page-corrupted",
        "title": "收费标准.pdf",
        "content": "%PDF-1.7\n3 0 obj\nstream\n错误二进制",
        "notebook_id": "remote-notebook",
        "source_type": None,
        "source_id": None,
        "source_pipeline_version": None,
        "index_status": "current",
    }
    state = RemoteRAGSyncState(tmp_path / "remote-state.json")
    state.update_document(
        "node-1",
        remote_page_id="page-corrupted",
        status="imported",
        title="收费标准.pdf",
        published_content_hash=expected_hash,
    )
    importer = DingTalkRemoteRAGImporter(storage, client, state)

    before = await importer.audit_remote_integrity(write_report=True)
    repaired = await importer.repair_remote_integrity()

    assert before["content_issue_count"] == 1
    assert before["metadata_issue_count"] == 1
    assert Path(before["report_path"]).is_file()
    assert repaired["status"] == "repaired"
    assert repaired["after"]["issue_count"] == 0
    assert client.pages["page-corrupted"]["content"] == expected_content
    assert client.pages["page-corrupted"]["source_type"] == "dingtalk"


@pytest.mark.asyncio
async def test_remote_import_adopts_unique_untracked_page_instead_of_creating_duplicate(
    tmp_path,
    monkeypatch,
):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_backup_dir", str(tmp_path / "backups"))
    prepare_latest_entry(
        storage,
        latest_markdown("这是需要覆盖老师服务器同名旧页面的最新版完整正文内容。"),
    )
    client = FakeRemoteClient()
    client.pages["old-page"] = {
        "id": "old-page",
        "title": "收费标准.pdf",
        "content": "旧版正文",
        "notebook_id": "remote-notebook",
        "index_status": "current",
    }
    state = RemoteRAGSyncState(tmp_path / "remote-state.json")
    importer = DingTalkRemoteRAGImporter(storage, client, state)

    result = await importer.import_manifest(snapshot=True)

    assert result["imported"] == 0
    assert result["updated"] == 1
    assert result["adopted_existing"] == 1
    assert client.creates == 0
    assert client.updates == 1
    assert state.document("node-1")["remote_page_id"] == "old-page"
    assert state.document("node-1")["adopted_existing"] is True


@pytest.mark.asyncio
async def test_remote_import_does_not_adopt_ambiguous_same_title_pages(
    tmp_path,
    monkeypatch,
):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_backup_dir", str(tmp_path / "backups"))
    prepare_latest_entry(
        storage,
        latest_markdown("这是无法与两个同名旧页安全对应的最新版完整正文内容。"),
    )
    client = FakeRemoteClient()
    for page_id in ("old-page-1", "old-page-2"):
        client.pages[page_id] = {
            "id": page_id,
            "title": "收费标准.pdf",
            "content": f"旧版正文-{page_id}",
            "notebook_id": "remote-notebook",
            "index_status": "current",
        }
    importer = DingTalkRemoteRAGImporter(
        storage,
        client,
        RemoteRAGSyncState(tmp_path / "remote-state.json"),
    )

    result = await importer.import_manifest(snapshot=False)

    assert result["imported"] == 1
    assert result["adopted_existing"] == 0
    assert client.creates == 1
    assert client.updates == 0


@pytest.mark.asyncio
async def test_remote_target_inspection_is_read_only(tmp_path, monkeypatch):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_backup_dir", str(tmp_path / "backups"))
    client = FakeRemoteClient()
    client.pages["existing"] = {
        "id": "existing",
        "title": "现有页面",
        "content": "现有内容",
        "notebook_id": "remote-notebook",
    }
    importer = DingTalkRemoteRAGImporter(
        storage,
        client,
        RemoteRAGSyncState(tmp_path / "remote-state.json"),
    )

    result = await importer.inspect_remote_target()

    assert result["notebook_name"] == "钉钉知识库"
    assert result["page_count"] == 1
    assert result["read_only"] is True
    assert client.creates == 0
    assert client.updates == 0
    assert client.indexes == 0


@pytest.mark.asyncio
async def test_remote_search_verification_requires_target_notebook_result(
    tmp_path,
    monkeypatch,
):
    storage = DingTalkLocalStorage(tmp_path / "dingtalk")
    monkeypatch.setattr(settings, "remote_rag_notebook_id", "")
    monkeypatch.setattr(settings, "remote_rag_notebook_name", "钉钉知识库")
    client = FakeRemoteClient()
    client.pages["target-page"] = {
        "id": "target-page",
        "title": "MC700参数配置",
        "content": "参数修改步骤",
        "notebook_id": "remote-notebook",
    }
    importer = DingTalkRemoteRAGImporter(
        storage,
        client,
        RemoteRAGSyncState(tmp_path / "remote-state.json"),
    )

    result = await importer.verify_remote_search(["MC700"])

    assert result["passed"] is True
    assert result["checks"][0]["target_results"][0]["id"] == "target-page"
