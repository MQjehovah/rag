"""P22 的真实 SQLite 升级回归测试。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "c8d9e0f1a2b3_p22_drop_legacy_ko_tables.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("p22_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_removes_unnamed_ko_fk_and_keeps_evidence_links_writable(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'p22.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            "CREATE TABLE knowledge_objects (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE evidence_items (id VARCHAR(36) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE evidence_links (
                id VARCHAR(36) PRIMARY KEY,
                card_id VARCHAR(36),
                legacy_ko_id VARCHAR(36),
                claim_id VARCHAR(36),
                evidence_id VARCHAR(36) NOT NULL,
                link_type VARCHAR(32) NOT NULL,
                confidence FLOAT,
                created_at DATETIME,
                FOREIGN KEY(legacy_ko_id) REFERENCES knowledge_objects(id),
                FOREIGN KEY(evidence_id) REFERENCES evidence_items(id) ON DELETE CASCADE
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_elink_ko_evidence "
            "ON evidence_links (legacy_ko_id, evidence_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_evidence_links_legacy_ko_id "
            "ON evidence_links (legacy_ko_id)"
        )

        migration = _load_migration()
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert not inspector.has_table("knowledge_objects")
        assert "legacy_ko_id" not in {
            column["name"] for column in inspector.get_columns("evidence_links")
        }
        assert all(
            fk.get("referred_table") != "knowledge_objects"
            for fk in inspector.get_foreign_keys("evidence_links")
        )

        connection.execute(
            sa.text("INSERT INTO evidence_items (id) VALUES ('ev-1')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO evidence_links "
                "(id, evidence_id, link_type) VALUES ('link-1', 'ev-1', 'supports')"
            )
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
