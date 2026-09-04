# -*- coding: utf-8 -*-
"""Phase 9A seed：幂等写入 CONTRACT §3 全部冻结数据（CONTRACT §3.3 来源壳）。

用法：
    cd backend
    python phase9a/seed.py --db <绝对 sqlite 路径>

- 开库前若文件不存在可用 alembic 已建好的库（先跑 bootstrap_db.py）；
- 幂等：可重复执行，不报错；user_groups 每次 sync 重建；
- bcrypt 用 passlib.context.CryptContext(schemes=["bcrypt"])；
- 结束后打印 frozen 摘要。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证从仓库根或 backend/ 启动都能 import。
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path.resolve().as_posix()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 9A seed")
    parser.add_argument("--db", required=True, help="绝对 sqlite 路径")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"[phase9a] DB 不存在，请先运行 bootstrap_db.py: {db_path}", file=sys.stderr)
        return 2

    import os
    os.environ["DATABASE_URL"] = _sqlite_url(db_path)

    from sqlalchemy import create_engine, event

    from app.models.database import get_session, init_db
    from phase9a import fixtures

    engine = create_engine(_sqlite_url(db_path),
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute("PRAGMA busy_timeout=8000")

    init_db(engine)
    db = get_session(engine)
    try:
        summary = fixtures.apply_seed(db)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()
        engine.dispose()

    print("[phase9a] frozen seed applied")
    print(f"[phase9a] usernames   = {summary['usernames']}")
    print(f"[phase9a] wiki_ids    = {summary['wiki_ids']}")
    print(f"[phase9a] page_ids    = {summary['page_ids']}")
    print(f"[phase9a] evidence_id = {summary['evidence_ids']}")
    print(f"[phase9a] workspaces  = {summary['workspaces']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
