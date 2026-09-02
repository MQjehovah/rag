"""P10-BE-02：Manifest 回填脚本测试（幂等）。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.database import get_engine, init_db


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "v3_backfill_dingtalk_sourceitems.py"


def _run_script(db_url, dingtalk_dir, dry_run=False):
    env = dict(
        __import__("os").environ,
        DATABASE_URL=db_url,
        DINGTALK_LOCAL_STORAGE_DIR=str(dingtalk_dir),
    )
    cmd = [sys.executable, str(SCRIPT)]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(SCRIPT.parent.parent))
    return result


@pytest.fixture()
def backfill_db(tmp_path):
    url = f"sqlite:///{(tmp_path / 'backfill.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    engine.dispose()
    return url


def test_backfill_idempotent(backfill_db, tmp_path, monkeypatch):
    # 造临时 manifest
    dingtalk_dir = tmp_path / "dingtalk"
    dingtalk_dir.mkdir()
    manifest = {
        "documents": [
            {"document_id": "doc1", "name": "a.pdf", "space_id": "s1",
             "markdown_hash": "h1", "rag_page_id": "page1",
             "source_url": "http://x", "dingtalk_path": "/a.pdf"},
        ],
        "last_inventory_at": "2026-01-01",
    }
    (dingtalk_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    # 跑两次，验证幂等
    r1 = _run_script(backfill_db, dingtalk_dir)
    assert r1.returncode == 0, r1.stderr

    r2 = _run_script(backfill_db, dingtalk_dir)
    assert r2.returncode == 0, r2.stderr

    engine = get_engine(backfill_db)
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM source_items")).scalar()
    engine.dispose()
    assert count == 1  # 两次跑只产生 1 条
