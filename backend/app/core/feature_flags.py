"""Persistent feature flag access with configuration defaults."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import RuntimeFeatureFlag


KNOWN_FLAGS = {
    "wiki_topic_enabled",
    # Phase 5：调度入口切 wiki.default pipeline（默认关闭）
    "wiki_pipeline_default_enabled",
    # 数据源平台
    "source_hub_enabled",
    "dingtalk_connector_enabled",
    "gitlab_connector_enabled",
}


def feature_enabled(db: Session, name: str) -> bool:
    if name not in KNOWN_FLAGS:
        raise ValueError(f"unknown feature flag: {name}")
    override = db.query(RuntimeFeatureFlag).filter(RuntimeFeatureFlag.name == name).first()
    return bool(override.enabled) if override else bool(getattr(settings, name))


def set_feature_flag(db: Session, name: str, enabled: bool, user_id: str | None) -> bool:
    if name not in KNOWN_FLAGS:
        raise ValueError(f"unknown feature flag: {name}")
    row = db.query(RuntimeFeatureFlag).filter(RuntimeFeatureFlag.name == name).first()
    if row is None:
        row = RuntimeFeatureFlag(name=name, enabled=enabled)
        db.add(row)
    row.enabled = enabled
    row.updated_by = user_id
    db.commit()
    # Keep the current process coherent with the persisted override; future
    # processes read the row from the database.
    setattr(settings, name, enabled)
    if name == "wiki_pipeline_default_enabled":
        # Phase 5.2：kill switch 进程内 TTL 缓存必须随 toggle 即时失效（运行时
        # 关闭后 scheduler 无需等 5s TTL）。函数内延迟 import，避免模块级硬依赖。
        try:
            from app.core.knowledge_compiler_v3.wiki_refresh_scheduler import (
                clear_kill_switch_cache,
            )
            clear_kill_switch_cache()
        except Exception:
            pass
    return bool(row.enabled)


def feature_snapshot(db: Session, names: tuple[str, ...] | None = None) -> dict[str, bool]:
    """返回一组开关的有效值（数据库覆盖优先于环境变量）。"""
    selected = names or tuple(sorted(KNOWN_FLAGS))
    return {name: feature_enabled(db, name) for name in selected}
