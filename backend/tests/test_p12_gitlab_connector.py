"""P12 GitLabConnector 测试（mock httpx，验证契约 + external_id + ACL + 删除）。"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.sources.gitlab import GitLabConnector, ALLOWED_EXTENSIONS, register_gitlab_connector
from app.sources.registry import is_registered


def _run(coro):
    return asyncio.run(coro)


class _FakeTransport(httpx.AsyncBaseTransport):
    """按 URL 返回假响应。"""
    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request):
        status, body = self.handler(request.url)
        import httpx as h
        return h.Response(status, text=body if isinstance(body, str) else "", request=request)


def _connector(handler):
    transport = _FakeTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    # 注入 client：GitLabConnector._get 用 httpx.AsyncClient()，这里 monkeypatch
    connector = GitLabConnector({"project_id": "42", "token": "tok", "base_url": "https://gitlab.example.com"})
    return connector


def _patch_get(monkeypatch, handler):
    """替换 GitLabConnector._get 为 fake。"""
    async def fake_get(self, url, params=None):
        status, body = handler(str(url))
        class R:
            status_code = status
            text = body if isinstance(body, str) else ""
            def json(self):
                import json
                return json.loads(self.text) if self.text else {}
        return R()
    monkeypatch.setattr(GitLabConnector, "_get", fake_get)


def test_register(monkeypatch):
    assert is_registered("gitlab") is False
    register_gitlab_connector()
    assert is_registered("gitlab") is True


def test_discover_two_scopes(monkeypatch):
    connector = GitLabConnector({"project_id": "42"})
    scopes = _run(connector.discover())
    assert len(scopes) == 2
    assert {s.kind for s in scopes} == {"repo", "wiki"}


def test_iter_repo_files_filters_extensions(monkeypatch):
    def handler(url):
        if "/repository/tree" in url:
            return 200, '[{"path":"README.md","id":"blob1"},{"path":"code.py","id":"blob2"},{"path":"doc.adoc","id":"blob3"}]'
        return 404, ""
    _patch_get(monkeypatch, handler)
    connector = GitLabConnector({"project_id": "42", "ref": "main"})

    async def collect():
        return [c async for c in connector.iter_changes(None)]
    changes = _run(collect())
    # code.py 被过滤（不在 ALLOWED_EXTENSIONS）
    assert len(changes) == 2
    assert all(".py" not in c.external_id for c in changes)
    # external_id 符合约定
    assert "project:42:repo:main:README.md" in [c.external_id for c in changes]


def test_fetch_repo_file_external_id_format(monkeypatch):
    def handler(url):
        if "/repository/files/" in url:
            return 200, "# 内容"
        return 404, ""
    _patch_get(monkeypatch, handler)
    connector = GitLabConnector({"project_id": "42", "ref": "main"})
    item = _run(connector.fetch_item("project:42:repo:main:doc.md"))
    assert item.content == "# 内容"
    assert item.content_hash
    assert item.acl_scope == {"project_id": "42"}


def test_fetch_wiki_404_deleted(monkeypatch):
    def handler(url):
        return 404, ""
    _patch_get(monkeypatch, handler)
    connector = GitLabConnector({"project_id": "42"})
    item = _run(connector.fetch_item("project:42:wiki:some-slug"))
    assert item.deleted is True


def test_fetch_acl_fail_closed_when_no_project(monkeypatch):
    connector = GitLabConnector({})
    acl = _run(connector.fetch_acl("anything"))
    assert acl.resolve_failed is True


def test_fetch_acl_returns_project_scope(monkeypatch):
    connector = GitLabConnector({"project_id": "42"})
    acl = _run(connector.fetch_acl("anything"))
    assert acl.resolve_failed is False
    assert acl.scope == "project:42"
