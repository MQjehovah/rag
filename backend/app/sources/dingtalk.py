from typing import Any, Dict, Tuple

from app.config import settings
from app.sources.base import DataSource


class DingTalkSource(DataSource):
    key = "dingtalk"
    name = "钉钉知识库"
    description = "同步钉钉知识库文档为笔记（适配现有同步能力）"

    def enabled(self) -> bool:
        return bool(settings.dingtalk_app_key and settings.dingtalk_app_secret)

    def config_summary(self) -> str:
        return "已配置" if self.enabled() else "未配置"

    async def test(self) -> Tuple[bool, str]:
        if not self.enabled():
            return False, "钉钉应用未配置（dingtalk_app_key/secret）"
        return True, "配置存在，可在知识库页面发起同步验证"

    async def sync(self, status: Dict[str, Any], params: Dict[str, Any] = None) -> None:
        from app.api.dingtalk import SYNC_STATUS, _do_sync
        from app.core.ingest import _get_or_create_notebook
        from app.models.database import get_engine, init_db

        engine = get_engine(settings.database_url)
        init_db(engine)
        notebook_id = _get_or_create_notebook(engine, "钉钉知识库")
        status["running"] = True
        status["message"] = "启动钉钉知识库同步..."
        await _do_sync(notebook_id)
        status["running"] = bool(SYNC_STATUS.get("running"))
        status["processed"] = SYNC_STATUS.get("imported", 0)
        status["total"] = SYNC_STATUS.get("total", 0)
        status["message"] = (
            SYNC_STATUS.get("progress")
            or f"钉钉同步完成：导入 {SYNC_STATUS.get('imported', 0)}，失败 {SYNC_STATUS.get('errors', 0)}"
        )
