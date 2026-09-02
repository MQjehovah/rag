"""GitLab Connector（P12，V3 计划 7）。

第二个 Connector，证明 P9 抽象可复用。首版只接高价值、低噪声内容：
1. Repository 中的 Markdown / AsciiDoc / 文本说明
2. GitLab Wiki

external_id 约定（V3 7.2）：
- repo:   project:{project_id}:repo:{ref}:{path}
- wiki:   project:{project_id}:wiki:{slug}

Token 只授予 read_api / read_repository，不进 config 明文（走 secret_ref 或环境变量）。
"""
from __future__ import annotations

import hashlib
import logging
from typing import AsyncIterator

import httpx

from app.sources.schemas import (
    ConnectionTestResult,
    NormalizedSourceItem,
    SourceACL,
    SourceAttachment,
    SourceChange,
    SourceScope,
)

logger = logging.getLogger(__name__)

# 允许的文件扩展名（低噪声内容）
ALLOWED_EXTENSIONS = {".md", ".markdown", ".adoc", ".asciidoc", ".txt", ".rst"}

# 429 退避重试
_RETRY_DELAYS = (0, 1, 2, 5, 10)


class GitLabConnector:
    key = "gitlab"
    name = "GitLab"

    def __init__(self, config: dict):
        self.config = config or {}
        self.base_url = (self.config.get("base_url") or "https://gitlab.com").rstrip("/")
        self.project_id = self.config.get("project_id") or ""
        self.ref = self.config.get("ref") or "main"
        self.token = self.config.get("token") or ""  # 实际应走 secret_ref，这里兼容直接传

    def _headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.token} if self.token else {}

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        """带 429/5xx 退避的 GET（P12-BE-05）。"""
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for delay in _RETRY_DELAYS:
                if delay:
                    import asyncio
                    await asyncio.sleep(delay)
                try:
                    resp = await client.get(url, headers=self._headers(), params=params)
                except httpx.HTTPError as exc:
                    last_exc = exc
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = Exception(f"HTTP {resp.status_code}")
                    continue
                return resp
        raise last_exc or RuntimeError("GitLab 请求失败")

    # ---- SourceConnector 契约 ----

    async def test_connection(self) -> ConnectionTestResult:
        if not self.token:
            return ConnectionTestResult(ok=False, message="缺少 token", error_code="AUTH_FAILED")
        if not self.project_id:
            return ConnectionTestResult(ok=False, message="缺少 project_id", error_code="BAD_REQUEST")
        try:
            resp = await self._get(f"{self.base_url}/api/v4/projects/{self.project_id}")
            if resp.status_code == 200:
                return ConnectionTestResult(ok=True, message="项目可访问")
            return ConnectionTestResult(ok=False, message=f"HTTP {resp.status_code}", error_code="AUTH_FAILED")
        except Exception as exc:
            return ConnectionTestResult(ok=False, message=str(exc), error_code="NETWORK_UNREACHABLE")

    async def discover(self) -> list[SourceScope]:
        # 单项目试点：只暴露 repo + wiki 两个 scope
        return [
            SourceScope(scope_id=f"project:{self.project_id}:repo", name="Repository", kind="repo"),
            SourceScope(scope_id=f"project:{self.project_id}:wiki", name="Wiki", kind="wiki"),
        ]

    async def iter_changes(self, cursor: dict | None) -> AsyncIterator[SourceChange]:
        # Repository 文件
        async for change in self._iter_repo_files(cursor):
            yield change
        # Wiki 页面
        async for change in self._iter_wiki_pages(cursor):
            yield change

    async def _iter_repo_files(self, cursor: dict | None) -> AsyncIterator[SourceChange]:
        """遍历 repository 树，只 yield 允许扩展名的文件。"""
        resp = await self._get(
            f"{self.base_url}/api/v4/projects/{self.project_id}/repository/tree",
            params={"ref": self.ref, "recursive": "true", "per_page": 100},
        )
        if resp.status_code != 200:
            return
        for entry in resp.json():
            path = entry.get("path", "")
            if not path or not any(path.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
                continue
            yield SourceChange(
                external_id=f"project:{self.project_id}:repo:{self.ref}:{path}",
                external_version=entry.get("id") or "",  # blob id
                deleted=False,
            )

    async def _iter_wiki_pages(self, cursor: dict | None) -> AsyncIterator[SourceChange]:
        """遍历 wiki 页面。"""
        resp = await self._get(
            f"{self.base_url}/api/v4/projects/{self.project_id}/wikis",
            params={"per_page": 100},
        )
        if resp.status_code != 200:
            return
        for page in resp.json():
            slug = page.get("slug", "")
            if not slug:
                continue
            yield SourceChange(
                external_id=f"project:{self.project_id}:wiki:{slug}",
                external_version=str(page.get("format") or ""),
                deleted=False,
            )

    async def fetch_item(self, external_id: str) -> NormalizedSourceItem:
        if ":wiki:" in external_id:
            return await self._fetch_wiki(external_id)
        return await self._fetch_repo_file(external_id)

    async def _fetch_repo_file(self, external_id: str) -> NormalizedSourceItem:
        # external_id: project:{id}:repo:{ref}:{path}
        parts = external_id.split(":", 4)
        path = parts[4] if len(parts) == 5 else ""
        # URL 编码 path
        from urllib.parse import quote
        resp = await self._get(
            f"{self.base_url}/api/v4/projects/{self.project_id}/repository/files/{quote(path, safe='')}/raw",
            params={"ref": self.ref},
        )
        content = resp.text if resp.status_code == 200 else ""
        return NormalizedSourceItem(
            connection_id=self.config.get("connection_id") or "unknown",
            source_type="gitlab",
            external_id=external_id,
            external_version="",  # 由 change 提供 blob id
            title=path.rsplit("/", 1)[-1],
            content=content,
            content_type=path.rsplit(".", 1)[-1] if "." in path else "text",
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_url=f"{self.base_url}/-/blob/{self.ref}/{path}",
            source_path=path,
            source_updated_at="",
            deleted=resp.status_code == 404,
            acl_scope={"project_id": self.project_id},
            metadata_json={"path": path, "ref": self.ref},
            attachments=[],
        )

    async def _fetch_wiki(self, external_id: str) -> NormalizedSourceItem:
        # external_id: project:{id}:wiki:{slug}
        slug = external_id.rsplit(":", 1)[-1]
        resp = await self._get(
            f"{self.base_url}/api/v4/projects/{self.project_id}/wikis/{slug}"
        )
        if resp.status_code != 200:
            return NormalizedSourceItem(
                connection_id=self.config.get("connection_id") or "unknown",
                source_type="gitlab", external_id=external_id,
                content="", deleted=True, acl_scope={"project_id": self.project_id},
            )
        data = resp.json()
        content = str(data.get("content") or "")
        return NormalizedSourceItem(
            connection_id=self.config.get("connection_id") or "unknown",
            source_type="gitlab",
            external_id=external_id,
            external_version=str(data.get("format") or ""),
            title=str(data.get("title") or slug),
            content=content,
            content_type="wiki",
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            source_url=f"{self.base_url}/-/wikis/{slug}",
            source_path=f"wiki:{slug}",
            source_updated_at=str(data.get("updated_at") or ""),
            deleted=False,
            acl_scope={"project_id": self.project_id},
            metadata_json={"slug": slug},
            attachments=[],
        )

    async def fetch_attachments(self, external_id: str) -> list[SourceAttachment]:
        return []  # 首版不接附件

    async def fetch_acl(self, external_id: str) -> SourceACL:
        # 项目级 ACL：project_id 作为 scope（P12-BE-04）
        if not self.project_id:
            return SourceACL(scope="", raw={}, resolve_failed=True)
        return SourceACL(
            scope=f"project:{self.project_id}",
            raw={"project_id": self.project_id},
            resolve_failed=False,
        )


def register_gitlab_connector() -> None:
    from app.sources.registry import register
    register("gitlab", lambda config: GitLabConnector(config))
