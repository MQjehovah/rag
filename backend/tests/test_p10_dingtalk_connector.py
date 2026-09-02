"""P10-BE-01/04：DingTalkConnector 适配器 + ACL fail-closed 测试。"""
from __future__ import annotations

import hashlib
import json
import asyncio

import pytest

from app.sources.dingtalk import DingTalkConnector, register_dingtalk_connector
from app.sources.registry import is_registered


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _restore_registry(monkeypatch):
    """每个测试前移除 dingtalk 注册，测试后恢复，保证 test_register 的隔离断言。

    其他测试（test_p9_sources_api 等）可能注册过 dingtalk connector，会污染
    test_register 开头「is_registered('dingtalk') is False」的断言。"""
    from app.sources import registry as _reg
    before = "dingtalk" in _reg._registry
    _reg._registry.pop("dingtalk", None)
    yield
    if before and "dingtalk" not in _reg._registry:
        from app.sources.dingtalk import register_dingtalk_connector
        register_dingtalk_connector()


@pytest.fixture()
def connector(tmp_path):
    # 造临时 manifest + storage（Phase 2.1：路径基于 storage.root，markdown 在 markdown_root 内）
    md_dir = tmp_path / "markdown"
    md_dir.mkdir(parents=True, exist_ok=True)
    md_file = md_dir / "doc1.md"
    # 合法旧管道 Frontmatter（严格身份：source_type/dingtalk_node_id/pipeline/converter）
    md_content = (
        '---\n'
        'source_type: "dingtalk"\n'
        'dingtalk_node_id: "doc1"\n'
        'conversion_pipeline_version: "dingtalk-markdown-pipeline-v24"\n'
        'converter: "hybrid_pdf"\n'
        '---\n\n# 正文内容'
    )
    md_file.write_bytes(md_content.encode("utf-8"))
    md_hash = hashlib.sha256(md_file.read_bytes()).hexdigest()
    # legacy converted：需要可验证 raw（Phase 2.3/2.4 converted 严格 raw 校验）
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / "doc1.pdf"
    raw_bytes = b"%PDF legacy raw"
    raw_file.write_bytes(raw_bytes)
    source_file_hash = hashlib.sha256(raw_bytes).hexdigest()
    manifest = {
        "documents": [
            {
                "document_id": "doc1", "name": "Service policy.pdf",
                "space_id": "space1", "space_name": "服务政策",
                "source_status": "active", "source_updated_at": "2026-01-01",
                "status": "converted",  # 旧字段 → legacy converted（inferred）
                "markdown_path": "markdown/doc1.md",  # 相对路径，在 markdown_root 内
                "markdown_hash": md_hash,
                "raw_path": "raw/doc1.pdf",
                "source_file_hash": source_file_hash,
            },
            {
                "document_id": "doc2", "name": "已删除文档.pdf",
                "space_id": "space1", "space_name": "服务政策",
                "source_status": "deleted",
                "markdown_hash": "hash2",
            },
        ],
        "last_inventory_at": "2026-01-01",
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    from app.core.dingtalk_storage import DingTalkLocalStorage
    storage = DingTalkLocalStorage(root=tmp_path)
    return DingTalkConnector({"connection_id": "conn1"}, storage=storage)


def test_register(monkeypatch):
    # 导入不自动注册；显式 register 后 is_registered
    assert is_registered("dingtalk") is False
    register_dingtalk_connector()
    assert is_registered("dingtalk") is True


def test_test_connection(connector, monkeypatch):
    # 隔离外部网络：无凭证时 test_connection 返回凭证缺失（fail closed）
    from app.config import settings
    monkeypatch.setattr(settings, "dingtalk_app_key", "")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "")
    result = _run(connector.test_connection())
    assert result.ok is False
    assert result.error_code == "CREDENTIALS_MISSING"


def test_test_connection_remote_success(connector, monkeypatch):
    # mock 远程空间列表，验证「凭证 → token → 空间 → 本地状态」链
    from app.config import settings
    monkeypatch.setattr(settings, "dingtalk_app_key", "key")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "secret")

    class FakeClient:
        async def list_workspaces(self):
            return [{"id": "space1", "name": "服务政策"}]

    monkeypatch.setattr(
        "app.core.dingtalk.DingTalkClient", lambda: FakeClient()
    )
    result = _run(connector.test_connection())
    assert result.ok is True


def test_discover(connector, monkeypatch):
    # 无凭证时回退本地 manifest 已见空间
    from app.config import settings
    monkeypatch.setattr(settings, "dingtalk_app_key", "")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "")
    scopes = _run(connector.discover())
    assert len(scopes) == 1
    assert scopes[0].scope_id == "space1"


def test_iter_changes(connector):
    async def collect():
        return [c async for c in connector.iter_changes(None)]
    changes = _run(collect())
    assert len(changes) == 2
    assert changes[0].external_id == "doc1"
    assert changes[0].deleted is False
    assert changes[1].deleted is True


def test_fetch_item(connector):
    item = _run(connector.fetch_item("doc1"))
    assert item.external_id == "doc1"
    assert item.title == "Service policy.pdf"
    assert "# 正文内容" in item.content  # 全文含正文
    assert item.metadata_json.get("manifest_is_markdown") is True  # 严格身份通过
    assert item.content_hash  # 非空


def test_fetch_item_deleted(connector):
    item = _run(connector.fetch_item("doc2"))
    assert item.deleted is True


def test_fetch_acl_fail_closed(connector):
    # 有 space_id → 正常
    acl = _run(connector.fetch_acl("doc1"))
    assert acl.resolve_failed is False
    assert acl.scope == "space1"

    # 无 space_id → fail closed（构造一个无 space_id 的文档）
    connector2 = DingTalkConnector({"connection_id": "conn1"})
    # doc2 有 space_id，这里直接测 fetch_acl 对不存在文档的行为
    acl2 = _run(connector2.fetch_acl("nonexistent"))
    assert acl2.resolve_failed is True
