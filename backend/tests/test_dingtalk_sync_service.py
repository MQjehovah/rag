import pytest

from app.config import settings
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.dingtalk_sync_service import DingTalkSyncService, DingTalkSyncState


def make_document(node_id="node-sync"):
    return {
        "id": node_id,
        "title": "同步入口测试.md",
        "extension": "md",
        "node_type": "FILE",
        "space_id": "space-001",
        "space_name": "产品知识库",
        "path": "同步入口测试.md",
    }


def test_sync_state_prevents_concurrent_tasks_and_can_start_again():
    state = DingTalkSyncState()
    first = state.begin(
        mode="selected",
        notebook_name="钉钉知识库",
        space_id=None,
        total=1,
    )

    with pytest.raises(RuntimeError, match="同步正在进行中"):
        state.begin(
            mode="full",
            notebook_name="钉钉知识库",
            space_id="space-001",
        )

    state.finish(success=True, progress="完成")
    second = state.begin(
        mode="full",
        notebook_name="钉钉知识库",
        space_id="space-001",
    )

    assert first["task_id"] != second["task_id"]
    assert second["running"] is True
    assert second["stage"] == "queued"


def test_running_checkpoint_is_marked_interrupted_after_process_restart(tmp_path):
    state_path = tmp_path / "sync-task.json"
    state = DingTalkSyncState(state_path)
    task = state.begin(
        mode="selected",
        notebook_name="钉钉知识库",
        space_id=None,
        total=1,
        request={
            "original_mode": "selected",
            "selected_docs": [make_document()],
        },
    )
    state.update(stage="conversion", progress="正在转换Markdown", percent=45)

    recovered = DingTalkSyncState(state_path)

    status = recovered.snapshot()
    assert status["task_id"] == task["task_id"]
    assert status["running"] is False
    assert status["stage"] == "interrupted"
    assert status["recoverable"] is True
    assert status["interrupted_at"]
    assert recovered.request_snapshot()["selected_docs"][0]["id"] == "node-sync"
    assert "conversion" in status["progress"]


def test_retry_plan_uses_manifest_stage_without_repeating_successful_steps(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        settings,
        "dingtalk_local_storage_dir",
        str(tmp_path / "dingtalk"),
    )
    storage = DingTalkLocalStorage()
    download_doc = make_document("node-download")
    conversion_doc = make_document("node-conversion")
    rag_doc = make_document("node-rag")
    storage.record_inventory([download_doc, conversion_doc, rag_doc])
    storage.update_document_status(download_doc, "failed", error="网络超时")
    storage.persist_raw_file(conversion_doc, b"conversion body", extension="md")
    storage.update_document_status(
        conversion_doc,
        "conversion_failed",
        error="临时解析失败",
    )
    storage.persist_raw_file(rag_doc, b"rag body", extension="md")
    storage.persist_markdown_file(rag_doc, "# RAG正文\n")
    rag_entry = next(
        entry for entry in storage.read_manifest()["documents"]
        if entry["document_id"] == "node-rag"
    )
    storage.update_rag_status(rag_entry, "failed", error="Embedding超时")

    state = DingTalkSyncState(tmp_path / "sync-task.json")
    state.begin(
        mode="selected",
        notebook_name="钉钉知识库",
        space_id=None,
        total=3,
        request={
            "original_mode": "selected",
            "notebook_name": "钉钉知识库",
            "selected_docs": [download_doc, conversion_doc, rag_doc],
        },
    )
    state.finish(success=True, progress="部分失败", errors=3)

    spec = DingTalkSyncService(state).build_retry_spec()

    assert spec["counts"] == {"download": 1, "conversion": 1, "rag": 1}
    assert spec["download_documents"][0]["id"] == "node-download"
    assert spec["conversion_ids"] == ["node-conversion"]
    assert spec["rag_ids"] == ["node-rag"]


def test_fatal_error_before_manifest_checkpoint_restarts_original_selection(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        settings,
        "dingtalk_local_storage_dir",
        str(tmp_path / "dingtalk"),
    )
    document = make_document("node-before-checkpoint")
    state = DingTalkSyncState(tmp_path / "sync-task.json")
    state.begin(
        mode="selected",
        notebook_name="钉钉知识库",
        space_id=None,
        total=1,
        request={
            "original_mode": "selected",
            "notebook_name": "钉钉知识库",
            "selected_docs": [document],
        },
    )
    state.finish(success=False, progress="客户端初始化失败", errors=1)

    spec = DingTalkSyncService(state).build_retry_spec()

    assert spec["restart"] is True
    assert spec["original_mode"] == "selected"
    assert spec["selected_docs"][0]["id"] == "node-before-checkpoint"


@pytest.mark.asyncio
async def test_retry_conversion_failure_reuses_local_raw_without_dingtalk(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        settings,
        "dingtalk_local_storage_dir",
        str(tmp_path / "dingtalk"),
    )
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'rag.db'}")
    storage = DingTalkLocalStorage()
    document = make_document("node-local-retry")
    storage.record_inventory([document])
    storage.persist_raw_file(document, b"local retry body", extension="md")
    storage.update_document_status(
        document,
        "conversion_failed",
        error="第一次转换临时失败",
    )

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("转换重试不应访问钉钉")

    class FakeImporter:
        def __init__(self, _db, storage=None):
            self.storage = storage

        async def import_manifest(self, notebook_name, document_ids, on_progress=None):
            entry = self.storage.read_manifest()["documents"][0]
            if on_progress:
                on_progress(entry, 1, 1, "imported", 1)
            self.storage.update_rag_status(
                entry,
                "imported",
                rag_page_id="page-retry",
                rag_chunk_count=1,
            )
            return {
                "notebook_id": "notebook-001",
                "notebook_name": notebook_name,
                "total": 1,
                "imported": 1,
                "skipped": 0,
                "failed": 0,
                "chunks": 1,
                "failures": [],
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.core.dingtalk_sync_service.DingTalkClient",
        ForbiddenClient,
    )
    monkeypatch.setattr(
        "app.core.dingtalk_sync_service.DingTalkRAGImporter",
        FakeImporter,
    )
    state = DingTalkSyncState(tmp_path / "sync-task.json")
    state.begin(
        mode="selected",
        notebook_name="钉钉知识库",
        space_id=None,
        total=1,
        request={
            "original_mode": "selected",
            "notebook_name": "钉钉知识库",
            "selected_docs": [document],
        },
    )
    state.finish(success=True, progress="转换失败", errors=1)
    service = DingTalkSyncService(state)
    spec = service.build_retry_spec()
    previous = state.snapshot()
    state.begin(
        mode="retry",
        notebook_name="钉钉知识库",
        space_id=None,
        total=1,
        request=state.request_snapshot(),
        retry_count=1,
        parent_task_id=previous["task_id"],
        retry_plan=spec["counts"],
    )

    await service.run(
        mode="retry",
        notebook_name="钉钉知识库",
        retry_spec=spec,
    )

    result = state.snapshot()
    assert result["stage"] == "completed"
    assert result["downloaded"] == 0
    assert result["converted"] == 1
    assert result["imported"] == 1
    assert result["retry_count"] == 1
    assert result["recoverable"] is False


@pytest.mark.asyncio
async def test_selected_sync_runs_download_conversion_and_rag_pipeline(
    monkeypatch,
    tmp_path,
):
    storage_root = tmp_path / "dingtalk"
    monkeypatch.setattr(settings, "dingtalk_local_storage_dir", str(storage_root))
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'rag.db'}")
    document = make_document()

    class FakeClient:
        async def download_selected_raw_files(self, documents, on_progress=None):
            storage = DingTalkLocalStorage()
            storage.record_inventory(documents)
            results = []
            for index, item in enumerate(documents, 1):
                metadata = storage.persist_raw_file(
                    item,
                    b"# local raw markdown\n\nbody\n",
                    extension="md",
                    mime_type="text/markdown",
                )
                results.append({**item, **metadata})
                if on_progress:
                    await on_progress(item, index)
            return results

        async def close(self):
            return None

    class FakeImporter:
        def __init__(self, _db, storage=None):
            self.storage = storage

        async def import_manifest(
            self,
            notebook_name,
            document_ids,
            on_progress=None,
        ):
            entry = self.storage.read_manifest()["documents"][0]
            if on_progress:
                on_progress(entry, 1, 1, "imported", 2)
            self.storage.update_rag_status(
                entry,
                "imported",
                rag_page_id="page-sync",
                rag_chunk_count=2,
            )
            return {
                "notebook_id": "notebook-001",
                "notebook_name": notebook_name,
                "total": 1,
                "imported": 1,
                "skipped": 0,
                "failed": 0,
                "chunks": 2,
                "failures": [],
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.core.dingtalk_sync_service.DingTalkClient",
        FakeClient,
    )
    monkeypatch.setattr(
        "app.core.dingtalk_sync_service.DingTalkRAGImporter",
        FakeImporter,
    )
    state = DingTalkSyncState()
    state.begin(
        mode="selected",
        notebook_name="钉钉知识库",
        space_id=None,
        total=1,
    )

    await DingTalkSyncService(state).run(
        mode="selected",
        notebook_name="钉钉知识库",
        selected_docs=[document],
    )

    result = state.snapshot()
    assert result["running"] is False
    assert result["stage"] == "completed"
    assert result["percent"] == 100
    assert result["downloaded"] == 1
    assert result["converted"] == 1
    assert result["imported"] == 1
    assert result["chunks"] == 2
    assert result["errors"] == 0
    entry = DingTalkLocalStorage().read_manifest()["documents"][0]
    assert entry["conversion_status"] == "converted"


@pytest.mark.asyncio
async def test_fatal_sync_error_is_visible_and_releases_running_state(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        settings,
        "dingtalk_local_storage_dir",
        str(tmp_path / "dingtalk"),
    )
    class FailingClient:
        _last_inventory_complete = False
        _last_inventory_scope_ids = []

        async def list_all_docs(self, _space_id, on_progress=None):
            raise RuntimeError("钉钉清单接口不可用")

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.core.dingtalk_sync_service.DingTalkClient",
        FailingClient,
    )
    state = DingTalkSyncState()
    state.begin(
        mode="full",
        notebook_name="钉钉知识库",
        space_id="space-001",
    )

    await DingTalkSyncService(state).run(
        mode="full",
        notebook_name="钉钉知识库",
        space_id="space-001",
    )

    result = state.snapshot()
    assert result["running"] is False
    assert result["stage"] == "failed"
    assert result["errors"] == 1
    assert "钉钉清单接口不可用" in result["progress"]
    assert result["error_details"][0]["stage"] == "pipeline"
