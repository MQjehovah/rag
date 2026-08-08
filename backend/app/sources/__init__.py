from typing import Dict, Optional

from app.sources.base import DataSource

SOURCES: Dict[str, DataSource] = {}


def register(source: DataSource) -> DataSource:
    SOURCES[source.key] = source
    return source


def get_source(key: str) -> Optional[DataSource]:
    return SOURCES.get(key)


from app.sources.jira import JiraSource  # noqa: E402
from app.sources.dingtalk import DingTalkSource  # noqa: E402

register(JiraSource())
register(DingTalkSource())
