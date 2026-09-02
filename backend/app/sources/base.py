"""Connector 契约（P9-BE-02，V3 计划 4.3）。

Connector 只负责外部通信与标准化，不直接操作 Card、Wiki 或图谱。
契约与文档 4.3 的 SourceConnector Protocol 一致。
"""
from __future__ import annotations

from typing import AsyncIterator, Protocol

from app.sources.schemas import (
    ConnectionTestResult,
    NormalizedSourceItem,
    SourceACL,
    SourceAttachment,
    SourceChange,
    SourceScope,
)


class SourceConnector(Protocol):
    key: str
    name: str

    async def test_connection(self) -> ConnectionTestResult: ...
    async def discover(self) -> list[SourceScope]: ...
    async def iter_changes(self, cursor: dict | None) -> AsyncIterator[SourceChange]: ...
    async def fetch_item(self, external_id: str) -> NormalizedSourceItem: ...
    async def fetch_attachments(self, external_id: str) -> list[SourceAttachment]: ...
    async def fetch_acl(self, external_id: str) -> SourceACL: ...
