"""独立数据源 Worker。用法：python scripts/source_worker.py"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.models.database import get_engine, get_session, init_db  # noqa: E402
from app.sources.bootstrap import ensure_builtin_connections  # noqa: E402
from app.sources.executor import execute_run  # noqa: E402
from app.sources.worker import claim_next_run, requeue_stale_runs  # noqa: E402


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        ensure_builtin_connections(db)
        requeue_stale_runs(db)
        while True:
            run = claim_next_run(db)
            if run is None:
                await asyncio.sleep(2)
                continue
            await execute_run(db, run)
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
