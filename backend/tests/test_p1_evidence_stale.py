"""P1-BE-08：文档 Hash 变化 → Evidence 标记 stale 测试。

判定单测覆盖 is_stale 的 None/相等/不等边界；
脚本集成测试覆盖回填、stale 标记、rejected 跳过、幂等、dry-run。
"""
from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.evidence_stale import is_stale
from app.models.database import Page, get_engine, init_db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "v3_evidence_stale.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("v3_evidence_stale", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 判定函数单测 ----------

def test_is_stale_none_source_not_stale():
    assert is_stale(None, "aaa") is False
    assert is_stale("", "aaa") is False


def test_is_stale_equal_hashes_not_stale():
    assert is_stale("aaa", "aaa") is False


def test_is_stale_mismatch_stale():
    assert is_stale("old", "new") is True
    assert is_stale("aaa", None) is True


# ---------- 脚本集成测试 ----------

def _add_evidence(conn, page_id, doc_hash, status="active"):
    eid = str(uuid.uuid4())
    conn.execute(text(
        "INSERT INTO evidence_items "
        "(id, source_page_id, source_chunk_id, evidence_type, content, "
        " content_hash, source_doc_hash, status) "
        "VALUES (:id, :page_id, NULL, 'text', 'c', 'ch', :doc_hash, :status)"
    ), {"id": eid, "page_id": page_id, "doc_hash": doc_hash, "status": status})
    return eid


@pytest.fixture()
def stale_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'stale_test.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    with engine.begin() as conn:
        p1 = str(uuid.uuid4())
        p2 = str(uuid.uuid4())
        conn.execute(text(
            "INSERT INTO pages (id, title, content, content_hash) VALUES (:id, 't1', 'c1', 'hash_a')"
        ), {"id": p1})
        conn.execute(text(
            "INSERT INTO pages (id, title, content, content_hash) VALUES (:id, 't2', 'c2', 'hash_b')"
        ), {"id": p2})
        # e1: 快照 == 当前 → active（保持）
        e1 = _add_evidence(conn, p1, "hash_a")
        # e2: 快照 != 当前 → stale
        e2 = _add_evidence(conn, p2, "hash_old")
        # e3: 快照 NULL → 回填 hash_b → active
        e3 = _add_evidence(conn, p2, None)
        # e4: rejected → 不参与判定
        e4 = _add_evidence(conn, p1, "hash_old", status="rejected")
    engine.dispose()
    monkeypatch.setattr("app.config.settings.database_url", url)
    return url, {"e1": e1, "e2": e2, "e3": e3, "e4": e4}


def _state(url):
    engine = get_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, source_doc_hash, status FROM evidence_items"
        )).fetchall()
    engine.dispose()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_stale_marks_only_changed_docs(stale_db):
    url, ids = stale_db
    mod = _load_script()
    assert mod.main() == 0

    s = _state(url)
    assert s[ids["e1"]] == ("hash_a", "active")     # 一致 → active
    assert s[ids["e2"]] == ("hash_old", "stale")    # 不一致 → stale
    assert s[ids["e3"]] == ("hash_b", "active")     # 回填 + active
    assert s[ids["e4"]] == ("hash_old", "rejected")  # rejected 跳过


def test_stale_idempotent(stale_db):
    url, ids = stale_db
    mod = _load_script()
    assert mod.main() == 0
    first = _state(url)
    assert mod.main() == 0
    assert _state(url) == first


def test_stale_dry_run_no_write(stale_db):
    url, ids = stale_db
    mod = _load_script()
    import sys
    sys.argv.append("--dry-run")
    try:
        assert mod.main() == 0
    finally:
        sys.argv.remove("--dry-run")

    # dry-run 后全部仍为初始状态（e3 的 source_doc_hash 也应为 NULL）
    s = _state(url)
    assert s[ids["e1"]] == ("hash_a", "active")
    assert s[ids["e2"]] == ("hash_old", "active")   # 未标记
    assert s[ids["e3"]] == (None, "active")          # 未回填
    assert s[ids["e4"]] == ("hash_old", "rejected")
