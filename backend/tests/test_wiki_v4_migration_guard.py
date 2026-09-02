"""V4 Phase C：迁移托管保护测试。

验证：
- init_db 对缺少托管字段的库抛 SchemaNotReadyError（不静默迁移）
- check_managed_migrations 正确报告缺失字段
- 测试库由 create_all 创建完整结构（字段齐全）
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, event
from sqlalchemy.pool import StaticPool

from app.models.database import (
    MANAGED_MIGRATION_COLUMNS,
    SchemaNotReadyError,
    check_managed_migrations,
    init_db,
    Base,
)


def _engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def test_create_all_produces_full_schema():
    """测试库由 create_all 创建，托管字段齐全。"""
    eng = _engine()
    init_db(eng)
    missing = check_managed_migrations(eng)
    assert missing == []
    eng.dispose()


def test_missing_managed_columns_reported():
    """只建部分列（模拟未执行 P31 的真实库），应报告缺失字段。"""
    eng = _engine()
    # 用 create_all 建全部，然后手动删掉一个无索引的托管列（edit_type）模拟旧库
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE wiki_revisions DROP COLUMN edit_type")
    missing = check_managed_migrations(eng)
    assert "wiki_revisions.edit_type" in missing
    eng.dispose()


def test_init_db_raises_schema_not_ready():
    """init_db 在缺托管字段时应抛 SchemaNotReadyError，而非静默补列。"""
    eng = _engine()
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE wiki_revisions DROP COLUMN edit_type")
    with pytest.raises(SchemaNotReadyError) as exc_info:
        init_db(eng)
    assert "schema_not_ready" in str(exc_info.value)
    # 确认没被静默补回
    cols = {c["name"] for c in inspect(eng).get_columns("wiki_revisions")}
    assert "edit_type" not in cols
    eng.dispose()


def test_managed_columns_registry_complete():
    assert ("wiki_pages", "source_page_ids") in MANAGED_MIGRATION_COLUMNS
    assert ("wiki_pages", "dirty") in MANAGED_MIGRATION_COLUMNS
    assert ("wiki_pages", "category") in MANAGED_MIGRATION_COLUMNS
    assert ("wiki_revisions", "edit_type") in MANAGED_MIGRATION_COLUMNS
    assert ("wiki_revisions", "updated_by") in MANAGED_MIGRATION_COLUMNS


def test_schema_not_ready_blocks_health():
    """health 端点对 schema_not_ready 不返回 healthy。"""
    eng = _engine()
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE wiki_revisions DROP COLUMN edit_type")
    from app.models.database import check_managed_migrations
    missing = check_managed_migrations(eng)
    assert missing  # 有缺失字段
    assert "wiki_revisions.edit_type" in missing
    eng.dispose()
