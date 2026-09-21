"""wiki_pages 向量列的存储与迁移测试。"""
from sqlalchemy import inspect, text

from app.models.database import get_engine, init_db


def _columns(engine, table="wiki_pages"):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_fresh_sqlite_adds_embedding_column_only(tmp_path):
    """全新库:有 JSON embedding 列,不建 Postgres 专用 embedding_vec 列。"""
    engine = get_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        init_db(engine)
        cols = _columns(engine)
        assert "embedding" in cols
        assert "embedding_vec" not in cols
    finally:
        engine.dispose()


def test_legacy_table_migration_preserves_rows_and_is_repeatable(tmp_path):
    """模拟旧表(无 embedding 列):迁移补列、保留数据,重复执行幂等。"""
    engine = get_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE wiki_pages ("
                "id VARCHAR(36) PRIMARY KEY, "
                "title VARCHAR(255) NOT NULL)"
            ))
            conn.execute(text(
                "INSERT INTO wiki_pages (id, title) VALUES ('legacy-1', '旧页面')"
            ))

        init_db(engine)

        assert "embedding" in _columns(engine)
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, title FROM wiki_pages")
            ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [("legacy-1", "旧页面")]

        # 再次执行不得报错(列已存在时 _migrate_schema 不会重复 ALTER)
        init_db(engine)
        assert "embedding" in _columns(engine)
    finally:
        engine.dispose()
