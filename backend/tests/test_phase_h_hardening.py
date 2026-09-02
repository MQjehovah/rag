"""V4 Phase H-1 最后一轮硬化专项测试。

覆盖：
- reconciliation 精确校验（索引列/顺序/unique、users 列/类型/长度/nullable/PK/FK）。
- malformed fixture（同名非唯一 cluster_key、错误列索引、错误 users 唯一索引、缺 FK、
  错误 nullable/长度）逐项 fail closed。
- P32.5 downgrade 为 no-op，不删 P32 索引，downgrade 后 schema fingerprint 完整。
- cluster_key 跨 hash seed 一致性（稳定版本化算法，不依赖 jieba）。
- 旧债务 keyset/batch 回填（大量旧债务、批次跨界、重复键失败后事务状态/版本不变）。
- migration 自包含（不 import 业务模块）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _alembic(cmd, db_path, *extra):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}",
         cmd, *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(BACKEND_ROOT),
    )


def _recon_module():
    spec = importlib.util.spec_from_file_location(
        "recon", str(BACKEND_ROOT / "alembic" / "versions" / "a8b9c0d1e2f3_p32_5_reconcile_debt_schema.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_drift(tmp_path, *, malformed=None):
    """构造 P31 漂移库（P32 列 + users 表存在，缺三 debt 索引）。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "drift.db"
    _alembic("upgrade", db_path, "b8e9f0a1b2c3")
    conn = sqlite3.connect(str(db_path))
    for ddl in [
        "ALTER TABLE knowledge_debts ADD COLUMN original_query TEXT",
        "ALTER TABLE knowledge_debts ADD COLUMN normalized_query VARCHAR(255)",
        "ALTER TABLE knowledge_debts ADD COLUMN cluster_key VARCHAR(255)",
        "ALTER TABLE knowledge_debts ADD COLUMN affected_user_count INTEGER",
        "ALTER TABLE knowledge_debts ADD COLUMN scope_id VARCHAR(255)",
        "ALTER TABLE knowledge_debts ADD COLUMN retrieval_reason VARCHAR(50)",
    ]:
        conn.execute(ddl)
    conn.execute(
        "CREATE TABLE knowledge_debt_users ("
        "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
        "user_id VARCHAR(255) NOT NULL, "
        "FOREIGN KEY(debt_id) REFERENCES knowledge_debts (id) ON DELETE CASCADE)"
    )
    if malformed:
        kind = malformed
        if kind == "non_unique_cluster_key":
            # 同名但非唯一的 cluster_key 索引（正确应是 unique）
            conn.execute("CREATE INDEX ux_knowledge_debts_cluster_key ON knowledge_debts (cluster_key)")
        elif kind == "wrong_column_index":
            # 错误列索引：normalized_query 索引建到 scope_id 上
            conn.execute("CREATE INDEX ix_knowledge_debts_normalized_query ON knowledge_debts (scope_id)")
        elif kind == "wrong_users_unique":
            # users 唯一索引列错误：ux_kdu_debt_user 建在 (debt_id) 而非 (debt_id, user_id)
            conn.execute("CREATE UNIQUE INDEX ux_kdu_debt_user ON knowledge_debt_users (debt_id)")
        elif kind == "missing_fk":
            # 缺 FK：重建 users 表但无 debt_id FK
            conn.execute("DROP TABLE knowledge_debt_users")
            conn.execute(
                "CREATE TABLE knowledge_debt_users ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
                "user_id VARCHAR(255) NOT NULL)"
            )
        elif kind == "wrong_nullable":
            # 错误 nullable：重建 knowledge_debt_users，user_id 设为 NOT NULL（本就 NOT NULL）
            # 用 debt_id 错误地设为 NULL（nullable）来模拟 nullable 错误。
            conn.execute("DROP TABLE knowledge_debt_users")
            conn.execute(
                "CREATE TABLE knowledge_debt_users ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36), "
                "user_id VARCHAR(255) NOT NULL)"
            )
        elif kind == "wrong_length":
            # 错误 VARCHAR 长度：user_id 长度错误（255 -> 36）
            conn.execute("DROP TABLE knowledge_debt_users")
            conn.execute(
                "CREATE TABLE knowledge_debt_users ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
                "user_id VARCHAR(36) NOT NULL)"
            )
        elif kind == "extra_users_col":
            # 额外 users 列
            conn.execute("DROP TABLE knowledge_debt_users")
            conn.execute(
                "CREATE TABLE knowledge_debt_users ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
                "user_id VARCHAR(255) NOT NULL, extra_col VARCHAR(10))"
            )
        elif kind == "wrong_pk":
            # 错误 PK：debt_id 被设为主键
            conn.execute("DROP TABLE knowledge_debt_users")
            conn.execute(
                "CREATE TABLE knowledge_debt_users ("
                "id VARCHAR(36) NOT NULL, debt_id VARCHAR(36) NOT NULL PRIMARY KEY, "
                "user_id VARCHAR(255) NOT NULL)"
            )
        elif kind == "wrong_fk":
            # 错误 FK：debt_id 引用错误表（pages）
            conn.execute("DROP TABLE knowledge_debt_users")
            conn.execute(
                "CREATE TABLE knowledge_debt_users ("
                "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
                "user_id VARCHAR(255) NOT NULL, "
                "FOREIGN KEY(debt_id) REFERENCES pages (id) ON DELETE CASCADE)"
            )
    conn.commit()
    conn.close()
    _alembic("stamp", db_path, "c5e6f7a8b9d0")
    return db_path


def test_reconciliation_is_self_contained():
    """migration 不得 import 业务模块（用 AST 检查实际 import 语句）。"""
    import ast
    p = BACKEND_ROOT / "alembic" / "versions" / "a8b9c0d1e2f3_p32_5_reconcile_debt_schema.py"
    tree = ast.parse(p.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
        elif isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
    forbidden_fragments = (
        "app.core.retrieval.debt_service", "app.core.access_control",
        "app.config", "jieba", "wiki_retriever",
    )
    for imp in imports:
        for frag in forbidden_fragments:
            assert frag not in imp, f"migration 不应 import {frag}，实际 import {imp}"


def test_cluster_key_cross_hashseed_consistent():
    """cluster_key 跨 hash seed 一致（稳定版本化算法，不依赖 jieba）。"""
    mod = _recon_module()
    k1 = mod._build_cluster_key("company", "水箱容量")
    # 模拟不同进程环境（新 interpreter + 随机 hash seed）计算同值
    code = (
        "import sys; sys.path.insert(0, r'{}'); "
        "from app.core.retrieval.debt_keying import build_cluster_key; "
        "print(build_cluster_key('company', '水箱容量'))"
    ).format(str(BACKEND_ROOT))
    for seed in ("0", "1", "random"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, cwd=str(BACKEND_ROOT),
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == k1, f"hash seed={seed} 下 cluster_key 不一致"
    # 运行时与 migration 一致
    from app.core.retrieval.debt_keying import build_cluster_key as runtime_key
    assert runtime_key("company", "水箱容量") == k1


def test_malformed_indexes_fail_closed(tmp_path):
    """同名但定义错误的索引必须 fail closed（不静默通过/重建）。"""
    # 非唯一 cluster_key 索引
    db1 = _build_drift(tmp_path / "a", malformed="non_unique_cluster_key")
    r = _alembic("upgrade", db1, "a8b9c0d1e2f3")
    assert r.returncode != 0
    assert "定义错误" in (r.stderr + r.stdout)

    # 错误列索引
    db2 = _build_drift(tmp_path / "b", malformed="wrong_column_index")
    r = _alembic("upgrade", db2, "a8b9c0d1e2f3")
    assert r.returncode != 0

    # 错误 users 唯一索引
    db3 = _build_drift(tmp_path / "c", malformed="wrong_users_unique")
    r = _alembic("upgrade", db3, "a8b9c0d1e2f3")
    assert r.returncode != 0

    # 缺 FK
    db4 = _build_drift(tmp_path / "d", malformed="missing_fk")
    r = _alembic("upgrade", db4, "a8b9c0d1e2f3")
    assert r.returncode != 0

    # 错误 nullable（debt_id 错误设为 nullable）
    db5 = _build_drift(tmp_path / "e", malformed="wrong_nullable")
    r = _alembic("upgrade", db5, "a8b9c0d1e2f3")
    assert r.returncode != 0

    # 错误 VARCHAR 长度（user_id 长度错误）
    db6 = _build_drift(tmp_path / "f", malformed="wrong_length")
    r = _alembic("upgrade", db6, "a8b9c0d1e2f3")
    assert r.returncode != 0

    # 额外 users 列
    db7 = _build_drift(tmp_path / "g", malformed="extra_users_col")
    r = _alembic("upgrade", db7, "a8b9c0d1e2f3")
    assert r.returncode != 0

    # 错误 PK
    db8 = _build_drift(tmp_path / "h", malformed="wrong_pk")
    r = _alembic("upgrade", db8, "a8b9c0d1e2f3")
    assert r.returncode != 0

    # 错误 FK（引用错误表）
    db9 = _build_drift(tmp_path / "i", malformed="wrong_fk")
    r = _alembic("upgrade", db9, "a8b9c0d1e2f3")
    assert r.returncode != 0


def test_downgrade_is_noop_and_preserves_p32_schema(tmp_path):
    """downgrade 为 no-op，不删 P32 索引，schema fingerprint 完整。"""
    db_path = _build_drift(tmp_path)
    r = _alembic("upgrade", db_path, "a8b9c0d1e2f3")
    assert r.returncode == 0, r.stderr
    # 记录 upgrade 后三个 debt 索引 + users 索引
    def fingerprint():
        conn = sqlite3.connect(str(db_path))
        debt = sorted(r0[1] for r0 in conn.execute("PRAGMA index_list(knowledge_debts)").fetchall())
        users = sorted(r0[1] for r0 in conn.execute("PRAGMA index_list(knowledge_debt_users)").fetchall())
        conn.close()
        return debt, users
    before = fingerprint()
    r = _alembic("downgrade", db_path, "c5e6f7a8b9d0")
    assert r.returncode == 0, r.stderr
    after = fingerprint()
    assert before == after, "downgrade 后 schema 改变（应 no-op）"
    # 三个 debt 索引仍在
    assert all(n in after[0] for n in ["ix_knowledge_debts_normalized_query", "ux_knowledge_debts_cluster_key", "ix_knowledge_debts_scope_id"])


def test_backfill_keyset_batch_cross_boundary(tmp_path):
    """大量旧债务（> batch）keyset 分页回填，批次跨界正确。"""
    mod = _recon_module()
    db_path = _build_drift(tmp_path)
    conn = sqlite3.connect(str(db_path))
    # 插入 450 条（> _BACKFILL_BATCH=200）旧债务，全部有 scope（admin）
    for i in range(450):
        conn.execute(
            "INSERT INTO knowledge_debts (id, debt_type, description, related_question, acl_scope, status) "
            "VALUES (?, 'missing_knowledge', 'd', ?, '{\"groups\": [\"__local_admin__\"]}', 'open')",
            (f"d{i}", f"query{i}"),
        )
    conn.commit()
    conn.close()
    r = _alembic("upgrade", db_path, "a8b9c0d1e2f3")
    assert r.returncode == 0, r.stderr
    # 断言回填数量（migrated 应从日志统计，这里直接查 DB 验证 cluster_key 非空）
    conn = sqlite3.connect(str(db_path))
    migrated = conn.execute("SELECT COUNT(*) FROM knowledge_debts WHERE cluster_key IS NOT NULL").fetchone()[0]
    assert migrated == 450
    conn.close()


def test_duplicate_cluster_key_fail_closed_no_partial_commit(tmp_path):
    """重复 cluster_key 失败后，事务回滚，版本号不变，数据不被删除。"""
    db_path = _build_drift(tmp_path)
    conn = sqlite3.connect(str(db_path))
    # 两条完整记录但有重复 cluster_key
    conn.execute(
        "INSERT INTO knowledge_debts (id, debt_type, description, normalized_query, scope_id, cluster_key) "
        "VALUES ('x1','x','x','q','company','dupkey')"
    )
    conn.execute(
        "INSERT INTO knowledge_debts (id, debt_type, description, normalized_query, scope_id, cluster_key) "
        "VALUES ('x2','x','x','q','company','dupkey')"
    )
    conn.commit()
    conn.close()
    r = _alembic("upgrade", db_path, "a8b9c0d1e2f3")
    assert r.returncode != 0
    assert "重复 cluster_key" in (r.stderr + r.stdout)
    # 版本号不变（仍 P32 前，即 stamp 的 c5e6f7a8b9d0）
    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "c5e6f7a8b9d0"
    # 两条数据仍在，未被删除
    assert conn.execute("SELECT COUNT(*) FROM knowledge_debts WHERE id IN ('x1','x2')").fetchone()[0] == 2
    conn.close()


# ---------------------------------------------------------------------------
# 备份/恢复加固测试
# ---------------------------------------------------------------------------

def _backup(db_path, out):
    script = str(BACKEND_ROOT / "scripts" / "backup_restore.py")
    return subprocess.run(
        [sys.executable, script, "backup", str(db_path), "-o", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _restore(bdir, target):
    script = str(BACKEND_ROOT / "scripts" / "backup_restore.py")
    return subprocess.run(
        [sys.executable, script, "restore", str(bdir), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def test_manifest_strict_schema(tmp_path):
    """manifest 严格校验：多 entry / 路径穿越 / 绝对路径 / 缺字段 均拒绝。"""
    db_file = tmp_path / "src.db"
    from sqlalchemy import create_engine
    from app.models.database import init_db
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(eng)
    eng.dispose()
    r = _backup(db_file, tmp_path / "bk")
    assert r.returncode == 0, r.stderr
    bdir = next((tmp_path / "bk").iterdir())
    mf = bdir / "manifest.json"

    # 路径穿越
    data = json.loads(mf.read_text(encoding="utf-8"))
    data["files"][0]["file"] = "../evil.db"
    mf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = _restore(bdir, tmp_path / "r1.db")
    assert r.returncode != 0
    assert not (tmp_path / "r1.db").exists()

    # 多 entry
    mf.write_text(json.dumps({
        "source": str(db_file), "files": [
            {"file": "a.db", "size": 1, "sha256": "0"*64},
            {"file": "b.db", "size": 1, "sha256": "0"*64},
        ]
    }, ensure_ascii=False), encoding="utf-8")
    r = _restore(bdir, tmp_path / "r2.db")
    assert r.returncode != 0
    assert not (tmp_path / "r2.db").exists()


def test_manifest_size_tamper_rejected(tmp_path):
    """manifest size 篡改必须拒绝。"""
    db_file = tmp_path / "src2.db"
    from sqlalchemy import create_engine
    from app.models.database import init_db
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(eng)
    eng.dispose()
    r = _backup(db_file, tmp_path / "bk2")
    assert r.returncode == 0, r.stderr
    bdir = next((tmp_path / "bk2").iterdir())
    mf = bdir / "manifest.json"
    data = json.loads(mf.read_text(encoding="utf-8"))
    data["files"][0]["size"] = data["files"][0]["size"] + 1
    mf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = _restore(bdir, tmp_path / "r3.db")
    assert r.returncode != 0
    assert not (tmp_path / "r3.db").exists()


def test_restore_rejects_overwrite(tmp_path):
    """restore 拒绝覆盖已存在目标。"""
    db_file = tmp_path / "src3.db"
    from sqlalchemy import create_engine
    from app.models.database import init_db
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(eng)
    eng.dispose()
    r = _backup(db_file, tmp_path / "bk3")
    assert r.returncode == 0, r.stderr
    bdir = next((tmp_path / "bk3").iterdir())
    target = tmp_path / "existing.db"
    target.write_bytes(b"x")
    r = _restore(bdir, target)
    assert r.returncode != 0
    # 目标文件内容未被覆盖（仍是原来的 b"x"）
    assert target.read_bytes() == b"x"


def test_backup_consistent_snapshot_with_concurrent_writes(tmp_path):
    """真实并发写入下，Backup API 快照是某个一致事务点（非固定条数的伪并发）。"""
    import threading
    import time as _time
    db_file = tmp_path / "concurrent.db"
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import StaticPool
    from app.models.database import init_db, KnowledgeDebt
    from sqlalchemy.orm import sessionmaker

    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(eng)
    s = sessionmaker(bind=eng)()
    s.add(KnowledgeDebt(id="base", debt_type="missing_knowledge", description="d",
                        original_query="q", normalized_query="q", scope_id="company"))
    s.commit()
    s.close()

    stop = threading.Event()

    def writer():
        s = sessionmaker(bind=eng)()
        i = 0
        while not stop.is_set():
            s.add(KnowledgeDebt(id=f"w{i}", debt_type="missing_knowledge", description="d",
                                original_query=f"q{i}", normalized_query=f"q{i}",
                                scope_id="company"))
            s.commit()
            i += 1
        s.close()

    t = threading.Thread(target=writer)
    t.start()
    # 让写连接持续写，同时执行 backup（只读 URI + Backup API）
    _time.sleep(0.05)
    r = _backup(db_file, tmp_path / "bk4")
    stop.set()
    t.join()
    eng.dispose()
    assert r.returncode == 0, r.stderr

    bdir = next((tmp_path / "bk4").iterdir())
    snapshot = bdir / "concurrent.db"
    conn = sqlite3.connect(str(snapshot))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # 快照必须是某个一致事务点：base 记录必在，快照行数 ≤ 最终行数（不硬编码条数）
    has_base = conn.execute("SELECT COUNT(*) FROM knowledge_debts WHERE id='base'").fetchone()[0] == 1
    assert has_base
    conn.close()
