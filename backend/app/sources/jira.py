from typing import Any, Dict, Tuple

from app.config import settings
from app.sources.base import DataSource


class JiraSource(DataSource):
    key = "jira"
    name = "Jira 工单"
    description = "增量同步已解决的 Jira 工单（描述+讨论），LLM 分析后形成笔记并编译进 Wiki"

    def enabled(self) -> bool:
        return bool(settings.jira_enabled and settings.jira_url)

    def config_summary(self) -> str:
        return settings.jira_url or "未配置"

    async def test(self) -> Tuple[bool, str]:
        import httpx
        from app.core.jira import _auth_header
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=_auth_header()) as client:
                resp = await client.get(f"{settings.jira_url.rstrip('/')}/rest/api/2/serverInfo")
                resp.raise_for_status()
                info = resp.json()
                return True, f"连接成功（Jira Server {info.get('version', '?')}）"
        except Exception as e:
            return False, f"连接失败: {e}"

    async def sync(self, status: Dict[str, Any], params: Dict[str, Any] = None) -> None:
        from app.core.jira import sync_jira
        mode = "backfill" if (params or {}).get("mode") == "backfill" else "incremental"
        days = int((params or {}).get("days") or 0)
        await sync_jira(status, mode=mode, days=days)
