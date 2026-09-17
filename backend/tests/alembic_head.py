"""查询仓库当前 Alembic 唯一 head，供“升级到 head 后应对齐脚本”断言。

历史 revision 锚点（P44/P43/P38 等）不得使用本模块，必须继续硬编码。
"""
from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND = Path(__file__).resolve().parent.parent


def alembic_script_directory() -> ScriptDirectory:
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg)


def current_alembic_heads() -> list[str]:
    return list(alembic_script_directory().get_heads())


def current_alembic_head() -> str:
    """返回当前唯一 head。多 head 或零 head 时断言失败。"""
    heads = current_alembic_heads()
    assert len(heads) == 1, f"expected single alembic head, got {heads}"
    return heads[0]
