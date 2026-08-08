from typing import Any, Dict, Tuple


class DataSource:
    """Base class for data-source plugins.

    A plugin pulls raw items from an enterprise system (Jira, DingTalk, ...),
    then the common ingest pipeline analyzes them with the LLM, creates
    notes, and compiles them into the wiki.
    """

    key: str = ""
    name: str = ""
    description: str = ""

    def enabled(self) -> bool:
        return False

    def config_summary(self) -> str:
        return ""

    async def test(self) -> Tuple[bool, str]:
        return False, "未实现"

    async def sync(self, status: Dict[str, Any], params: Dict[str, Any] = None) -> None:
        raise NotImplementedError
