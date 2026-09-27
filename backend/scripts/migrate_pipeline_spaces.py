"""一次性迁移(幂等):
1) 实体化「默认空间」(WikiSpace);
2) 回填 wiki_pages.space_id = 默认空间;
3) Postgres: 把 title 的全局唯一改为 (space_id, title) 唯一。

用法(容器内): PYTHONPATH=/app python scripts/migrate_pipeline_spaces.py
"""
import uuid

from sqlalchemy import text

from app.config import settings
from app.models.database import (
    DEFAULT_SPACE_NAME,
    WikiPage,
    WikiSpace,
    get_engine,
    get_session,
    init_db,
)


def main() -> None:
    engine = get_engine(settings.database_url)
    init_db(engine)

    db = get_session(engine)
    try:
        space = db.query(WikiSpace).filter(WikiSpace.name == DEFAULT_SPACE_NAME).first()
        if space is None:
            space = WikiSpace(
                id=str(uuid.uuid4()),
                name=DEFAULT_SPACE_NAME,
                icon="📄",
                description="未指定空间的编译产物",
                position=0,
                group_id=None,
            )
            db.add(space)
            db.commit()
            db.refresh(space)
            print("[migrate] created default space:", space.id)
        else:
            print("[migrate] default space exists:", space.id)

        changed = (
            db.query(WikiPage)
            .filter(WikiPage.space_id.is_(None))
            .update({WikiPage.space_id: space.id}, synchronize_session=False)
        )
        db.commit()
        print("[migrate] backfilled wiki_pages.space_id:", changed)
    finally:
        db.close()

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE wiki_pages DROP CONSTRAINT IF EXISTS wiki_pages_title_key"))
            conn.execute(text("DROP INDEX IF EXISTS ix_wiki_pages_title"))
            conn.execute(text("DROP INDEX IF EXISTS uq_wiki_pages_space_title"))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_wiki_pages_space_title "
                "ON wiki_pages (space_id, title)"
            ))
        print("[migrate] unique index -> (space_id, title)")
    else:
        print("[migrate] skip unique-index rebuild (non-postgres dialect)")


if __name__ == "__main__":
    main()
