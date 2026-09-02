"""V4 Phase H-2 release gate 集成测试。

覆盖：
- preflight 对漂移副本全 PASS、对非漂移副本 FAIL；
- preflight baseline 保存 + 恢复副本 compare 通过；
- postflight reconciled / p33 两 stage 正确性；
- 备份恢复闭环（backup → restore → preflight 对比）；
- manifest 绝对路径/../缺字段/非法SHA/大小/多entry/重复entry/覆盖；
- WAL 显式设置 + 事件同步证明 backup 期间有成功提交。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = BACKEND_ROOT / "scripts"


def _run(cmd, *args):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(BACKEND_ROOT) + os.pathsep + str(SCRIPTS)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / cmd), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(BACKEND_ROOT),
    )


def _alembic(cmd, db_path, *extra):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url=sqlite:///{db_path.as_posix()}",
         cmd, *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(BACKEND_ROOT),
    )


def _build_drift(tmp_path, *, stamp=True):
    """构造 P31 漂移库（P32 列 + users 表存在，缺三 debt 索引）。

    stamp=True 时把版本标记为 P32（用于 postflight/迁移测试）；
    stamp=False 时保留 P31（用于 preflight 测试，preflight 必须在 stamp 前跑）。
    """
    db_path = tmp_path / "drift.db"
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    # 真实漂移形态：users 三索引已存在（P32 create_index 已执行），debt 三索引缺失。
    conn.execute("CREATE INDEX ix_knowledge_debt_users_debt_id ON knowledge_debt_users (debt_id)")
    conn.execute("CREATE INDEX ix_knowledge_debt_users_user_id ON knowledge_debt_users (user_id)")
    conn.execute("CREATE UNIQUE INDEX ux_kdu_debt_user ON knowledge_debt_users (debt_id, user_id)")
    # 6 条债务（1 可迁移 + 4 无 scope + 1 空 query），card_v3_enabled=1
    conn.execute("INSERT INTO runtime_feature_flags (name, enabled) VALUES ('card_v3_enabled', 1)")
    conn.execute("INSERT INTO runtime_feature_flags (name, enabled) VALUES ('source_hub_enabled', 1)")
    debts = [
        ("如何给 Titan 810 换轮胎", None),
        ("Titan 810 标准电池是多少?", None),
        ("如何安装 Skywalker50 电池", None),
        ("如何给 Titan 810 换轮胎", None),
        ("", None),
        ("nihao", '{"groups": ["__local_admin__"]}'),
    ]
    for i, (q, acl) in enumerate(debts):
        did = f"d{i+1}"
        conn.execute(
            "INSERT INTO knowledge_debts (id, debt_type, description, related_question, acl_scope, status) "
            "VALUES (?, 'missing_knowledge', 'd', ?, ?, 'open')",
            (did, q, acl),
        )
    # 填充保留表行数基线（executemany 快速插入）
    conn.execute("INSERT INTO notebooks (id, name, group_id) VALUES ('nb1','工程','engineering')")
    conn.execute("INSERT INTO source_connections (id, connector_key, name) VALUES ('sc1','gitlab','g')")
    conn.execute("INSERT INTO wiki_pages (id, title, status) VALUES ('w1','水箱','published')")
    for i in range(3):
        conn.execute(
            "INSERT INTO wiki_revisions (id, wiki_page_id, title, status) VALUES (?, 'w1', 'r', 'published')",
            (f"wr{i}",),
        )
    conn.executemany(
        "INSERT INTO pages (id, notebook_id, title, content) VALUES (?, 'nb1', ?, 'c')",
        [(f"p{i}", f"t{i}") for i in range(144)],
    )
    conn.executemany(
        "INSERT INTO page_chunks (id, page_id, chunk_index, content, content_type) VALUES (?, ?, 0, 'c', 'text')",
        [(f"c{i}", f"p{i % 144}") for i in range(5403)],
    )
    conn.executemany(
        "INSERT INTO evidence_items (id, source_page_id, evidence_type, content, status) VALUES (?, ?, 'text', 'c', 'active')",
        [(f"e{i}", f"p{i % 144}") for i in range(5403)],
    )
    conn.commit()
    conn.close()
    if stamp:
        _alembic("stamp", db_path, "c5e6f7a8b9d0")
    return db_path


def test_preflight_passes_drift(tmp_path):
    """漂移副本 preflight（stamp 前，版本 P31）应 PASS。"""
    db = _build_drift(tmp_path, stamp=False)
    r = _run("preflight_h2.py", "--db", str(db), "--report")
    assert r.returncode == 0, r.stderr + r.stdout
    data = json.loads(r.stdout)
    assert data["passed"] is True


def test_preflight_fails_wrong_version(tmp_path):
    """版本不是 P31 时 preflight FAIL。"""
    db = _build_drift(tmp_path, stamp=False)
    # 改版本号为 P32，模拟非漂移形态
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE alembic_version SET version_num='c5e6f7a8b9d0'")
    conn.commit()
    conn.close()
    r = _run("preflight_h2.py", "--db", str(db))
    assert r.returncode != 0


def _rebuild_debt_users_fk(db_path, fk_ddl):
    """重建 knowledge_debt_users 为给定（错误）FK 形态，验证 preflight fail closed。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE knowledge_debt_users")
    conn.execute(fk_ddl)
    conn.commit()
    conn.close()


def _preflight_fk_must_fail(db_path):
    r = _run("preflight_h2.py", "--db", str(db_path), "--report")
    assert r.returncode != 0, "错误 FK 应导致 preflight FAIL"
    data = json.loads(r.stdout)
    fk_checks = [c for c in data["checks"] if "CASCADE" in c["check"]]
    assert fk_checks, "应存在 CASCADE FK 检查项"
    assert all(not c["passed"] for c in fk_checks), f"FK 检查应 fail closed：{fk_checks}"


def test_preflight_fk_fail_closed_wrong_target_ondelete_column(tmp_path):
    """错误目标表 / 错误 ondelete / 错误 from 列，preflight 均 fail closed（结构化 FK 判定）。"""
    # 错误目标表（debt_id → pages.id 而非 knowledge_debts.id）
    db1 = _build_drift(tmp_path / "fk_target", stamp=False)
    _rebuild_debt_users_fk(db1,
        "CREATE TABLE knowledge_debt_users ("
        "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
        "user_id VARCHAR(255) NOT NULL, "
        "FOREIGN KEY(debt_id) REFERENCES pages (id) ON DELETE CASCADE)")
    _preflight_fk_must_fail(db1)

    # 错误 ondelete（NO ACTION 而非 CASCADE）
    db2 = _build_drift(tmp_path / "fk_ondelete", stamp=False)
    _rebuild_debt_users_fk(db2,
        "CREATE TABLE knowledge_debt_users ("
        "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
        "user_id VARCHAR(255) NOT NULL, "
        "FOREIGN KEY(debt_id) REFERENCES knowledge_debts (id) ON DELETE NO ACTION)")
    _preflight_fk_must_fail(db2)

    # 错误 from 列（user_id 作为 FK 列，而非 debt_id）
    db3 = _build_drift(tmp_path / "fk_column", stamp=False)
    _rebuild_debt_users_fk(db3,
        "CREATE TABLE knowledge_debt_users ("
        "id VARCHAR(36) NOT NULL PRIMARY KEY, debt_id VARCHAR(36) NOT NULL, "
        "user_id VARCHAR(255) NOT NULL, "
        "FOREIGN KEY(user_id) REFERENCES knowledge_debts (id) ON DELETE CASCADE)")
    _preflight_fk_must_fail(db3)


def test_preflight_baseline_compare_roundtrip(tmp_path):
    """preflight baseline 保存后，恢复副本 compare 通过。"""
    db = _build_drift(tmp_path, stamp=False)
    baseline = tmp_path / "baseline.json"
    r = _run("preflight_h2.py", "--db", str(db), "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr

    # 备份 + 恢复
    bkdir = tmp_path / "bk"
    r = _run("backup_restore.py", "backup", str(db), "-o", str(bkdir))
    assert r.returncode == 0, r.stderr
    bdir = next(bkdir.iterdir())
    restored = tmp_path / "restored.db"
    r = _run("backup_restore.py", "restore", str(bdir), str(restored))
    assert r.returncode == 0, r.stderr

    # 对恢复副本跑同一 preflight + compare
    r = _run("preflight_h2.py", "--db", str(restored), "--compare", str(baseline))
    assert r.returncode == 0, r.stderr + r.stdout


def test_postflight_reconciled_and_p33(tmp_path):
    """完整演练：preflight baseline → reconcile → postflight reconciled → p33 → postflight p33。"""
    db = _build_drift(tmp_path, stamp=False)
    baseline = tmp_path / "baseline.json"
    r = _run("preflight_h2.py", "--db", str(db), "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr

    r = _alembic("stamp", db, "c5e6f7a8b9d0")
    assert r.returncode == 0, r.stderr

    r = _alembic("upgrade", db, "a8b9c0d1e2f3")
    assert r.returncode == 0, r.stderr

    r = _run("postflight_h2.py", "--db", str(db), "--stage", "reconciled", "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr + r.stdout

    r = _alembic("upgrade", db, "d1e2f3a4b5c6")
    assert r.returncode == 0, r.stderr

    r = _run("postflight_h2.py", "--db", str(db), "--stage", "p33", "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr + r.stdout


def test_manifest_absolute_path_and_traversal(tmp_path):
    """manifest 绝对路径 / ../ 拒绝。"""
    db = _build_drift(tmp_path)
    bkdir = tmp_path / "bk2"
    r = _run("backup_restore.py", "backup", str(db), "-o", str(bkdir))
    assert r.returncode == 0, r.stderr
    bdir = next(bkdir.iterdir())
    mf = bdir / "manifest.json"
    data = json.loads(mf.read_text(encoding="utf-8"))

    # 绝对路径
    data["files"][0]["file"] = str(bdir / "evil.db")
    mf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = _run("backup_restore.py", "restore", str(bdir), str(tmp_path / "r1.db"))
    assert r.returncode != 0

    # 路径穿越
    data = json.loads(mf.read_text(encoding="utf-8"))
    data["files"][0]["file"] = "../evil.db"
    mf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = _run("backup_restore.py", "restore", str(bdir), str(tmp_path / "r2.db"))
    assert r.returncode != 0


def test_manifest_missing_field_duplicate_entries(tmp_path):
    """manifest 缺字段 / 重复 entry 拒绝。"""
    db = _build_drift(tmp_path)
    bkdir = tmp_path / "bk3"
    r = _run("backup_restore.py", "backup", str(db), "-o", str(bkdir))
    assert r.returncode == 0, r.stderr
    bdir = next(bkdir.iterdir())
    mf = bdir / "manifest.json"

    # 缺字段
    mf.write_text(json.dumps({"files": [{"file": "a.db", "size": 1}]}), encoding="utf-8")
    r = _run("backup_restore.py", "restore", str(bdir), str(tmp_path / "r3.db"))
    assert r.returncode != 0

    # 重复 entry（两个 files）
    mf.write_text(json.dumps({
        "source": str(db), "files": [
            {"file": "a.db", "size": 1, "sha256": "0"*64},
            {"file": "a.db", "size": 1, "sha256": "0"*64},
        ]
    }), encoding="utf-8")
    r = _run("backup_restore.py", "restore", str(bdir), str(tmp_path / "r4.db"))
    assert r.returncode != 0


def test_wal_snapshot_consistent_with_concurrent_commits(tmp_path):
    """WAL 显式设置 + 三阶段提交计数：backup 期间有成功提交，快照序号连续且一致。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.database import init_db, KnowledgeDebt

    db_file = tmp_path / "wal.db"
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    init_db(eng)

    # 显式设置 WAL（journal_mode 持久化在 DB 文件，后续连接均用 WAL）。
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.close()

    s = sessionmaker(bind=eng)()
    s.add(KnowledgeDebt(id="base", debt_type="missing_knowledge", description="d",
                        original_query="q", normalized_query="q", scope_id="company"))
    s.commit()
    s.close()

    # 持续 writer：每条记录一个事务，用 occurrence_count 作为单调递增序号。
    stop = threading.Event()
    lock = threading.Lock()
    commit_count = [0]

    def writer():
        s = sessionmaker(bind=eng)()
        i = 0
        while not stop.is_set():
            s.add(KnowledgeDebt(id=f"w{i}", debt_type="missing_knowledge", description="d",
                                original_query=f"q{i}", normalized_query=f"q{i}",
                                scope_id="company", occurrence_count=i))
            s.commit()
            with lock:
                commit_count[0] += 1
            i += 1
        s.close()

    t = threading.Thread(target=writer)
    t.start()

    # 等 writer 写活跃，确认 backup 前 WAL 文件存在且 size > 0。
    time.sleep(0.3)
    wal_file = Path(str(db_file) + "-wal")
    assert wal_file.exists(), "backup 前 WAL 文件应存在"
    assert wal_file.stat().st_size > 0, "backup 前 WAL 文件 size 应 > 0（存在未 checkpoint 内容）"

    with lock:
        before = commit_count[0]

    # backup（同步阻塞，期间 writer 持续提交）
    r = _run("backup_restore.py", "backup", str(db_file), "-o", str(tmp_path / "bkw"))

    with lock:
        during_end = commit_count[0]  # backup 返回后（writer 仍在写）

    stop.set()
    t.join()
    with lock:
        after = commit_count[0]  # writer 停止后
    eng.dispose()

    assert r.returncode == 0, r.stderr
    # 三阶段计数：backup 执行区间 [before, during_end] 内至少有一次成功提交。
    assert during_end > before, f"backup 期间应有成功提交（before={before}, during_end={during_end}）"
    assert after >= during_end, f"结束后提交计数不应回退（during_end={during_end}, after={after}）"

    bdir = next((tmp_path / "bkw").iterdir())
    snapshot = bdir / "wal.db"
    conn = sqlite3.connect(str(snapshot))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # base 记录必在（一致事务点）
    assert conn.execute("SELECT COUNT(*) FROM knowledge_debts WHERE id='base'").fetchone()[0] == 1
    # 快照序号连续：writer 的 occurrence_count 从 0 开始无空洞，证明快照停在合法提交边界。
    seqs = [r[0] for r in conn.execute(
        "SELECT occurrence_count FROM knowledge_debts "
        "WHERE id LIKE 'w%' ORDER BY occurrence_count"
    ).fetchall()]
    assert seqs, "快照应包含 writer 已提交记录"
    assert seqs == list(range(len(seqs))), f"快照序号不连续（存在空洞/跳过）：前 10 项 {seqs[:10]}"
    conn.close()


def test_postflight_p33_flag_baseline(tmp_path):
    """preflight baseline 记录 name→enabled；P33 后 legacy 消失、非 legacy 不变；篡改 enabled 则 FAIL。"""
    db = _build_drift(tmp_path, stamp=False)
    conn = sqlite3.connect(str(db))
    # 补齐 8 个 legacy flag（card_v3_enabled 已在）+ 非 legacy flag（source_hub_enabled 已在）
    for name in ("unified_retrieval_enabled", "debt_chat_enabled", "card_graph_enabled",
                 "layered_retrieval_enabled", "source_card_compile_enabled",
                 "legacy_debt_card_enabled", "legacy_readonly"):
        conn.execute("INSERT INTO runtime_feature_flags (name, enabled) VALUES (?, 1)", (name,))
    conn.execute("INSERT INTO runtime_feature_flags (name, enabled) VALUES ('dingtalk_connector_enabled', 1)")
    conn.commit()
    conn.close()

    baseline = tmp_path / "baseline.json"
    r = _run("preflight_h2.py", "--db", str(db), "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr

    # 完整迁移链：stamp P32 → reconciliation → P33
    assert _alembic("stamp", db, "c5e6f7a8b9d0").returncode == 0
    r = _alembic("upgrade", db, "a8b9c0d1e2f3")
    assert r.returncode == 0, r.stderr
    # reconciled 阶段 flag 不变
    r = _run("postflight_h2.py", "--db", str(db), "--stage", "reconciled", "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr + r.stdout

    r = _alembic("upgrade", db, "d1e2f3a4b5c6")
    assert r.returncode == 0, r.stderr
    # p33：8 legacy 消失 + 非 legacy 不变
    r = _run("postflight_h2.py", "--db", str(db), "--stage", "p33", "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr + r.stdout

    # 篡改非 legacy flag enabled → FAIL
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE runtime_feature_flags SET enabled=0 WHERE name='source_hub_enabled'")
    conn.commit()
    conn.close()
    r = _run("postflight_h2.py", "--db", str(db), "--stage", "p33", "--baseline", str(baseline), "--report")
    assert r.returncode != 0, "非 legacy flag enabled 被篡改应 FAIL"
    data = json.loads(r.stdout)
    nonlegacy = [c for c in data["checks"] if "非 legacy" in c["check"]]
    assert nonlegacy and all(not c["passed"] for c in nonlegacy), nonlegacy


def test_postflight_p33_users_columns_fail_closed(tmp_path):
    """users 精确列集合被验证：额外列应导致 p33 postflight FAIL。"""
    db = _build_drift(tmp_path, stamp=False)
    baseline = tmp_path / "baseline.json"
    r = _run("preflight_h2.py", "--db", str(db), "--baseline", str(baseline))
    assert r.returncode == 0, r.stderr
    assert _alembic("stamp", db, "c5e6f7a8b9d0").returncode == 0
    assert _alembic("upgrade", db, "a8b9c0d1e2f3").returncode == 0
    assert _alembic("upgrade", db, "d1e2f3a4b5c6").returncode == 0

    # 篡改 users 列集合（加额外列）
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE users ADD COLUMN extra_col VARCHAR(10)")
    conn.commit()
    conn.close()

    r = _run("postflight_h2.py", "--db", str(db), "--stage", "p33", "--baseline", str(baseline), "--report")
    assert r.returncode != 0, "users 列集合被篡改应 FAIL"
    data = json.loads(r.stdout)
    users_checks = [c for c in data["checks"] if "users" in c["check"]]
    assert users_checks and any(not c["passed"] for c in users_checks), users_checks


def test_runbook_static_rules():
    """runbook 命令静态校验：可执行路径、无占位符、无 -Force、postflight 均含 --baseline、统一 cwd。"""
    import re
    runbook = BACKEND_ROOT.parent / "docs" / "phase-h-2-runbook.md"
    text = runbook.read_text(encoding="utf-8")

    # 提取所有 powershell 代码块（```powershell ... ```）内的命令文本。
    blocks = re.findall(r"```powershell\s*\n(.*?)```", text, flags=re.DOTALL)
    assert blocks, "runbook 应包含 powershell 代码块"
    cmd_text = "\n".join(blocks)

    # 1. 不含错误路径 `\.venv`（必须以 `.\.venv` 形式，即 `\` 前是 `.`）。
    bad_venv = re.findall(r'(?<!\.)\\\.venv', text)
    assert bad_venv == [], f"runbook 含错误 python 路径（应写 .\\.venv）：{bad_venv}"

    # 2. 不含 `<timestamp>` 占位符。
    assert "<timestamp>" not in text, "runbook 仍含 <timestamp> 占位符，应改用 backup --result-json"

    # 3. 不含 `Move-Item -Force`。
    assert re.search(r'Move-Item[^\n]*\-Force', cmd_text) is None, "runbook 不得用 -Force 覆盖 quarantine / notes.db"

    # 4. postflight 命令均含 --baseline。
    postflight_lines = [ln for ln in text.splitlines() if "postflight_h2.py" in ln]
    assert postflight_lines, "runbook 应包含 postflight_h2.py 命令"
    for ln in postflight_lines:
        assert "--baseline" in ln, f"postflight 命令缺 --baseline：{ln.strip()}"

    # 5. 所有命令统一以 backend 为 cwd：命令内不得出现完整数据库路径 `backend\data\notes.db`
    #    / `backend/data/notes.db`（应写 `data\notes.db`），且含 `cd backend`。
    #    说明文字与 `$backendRoot\data` 目录安全校验（变量引用）不受影响。
    assert "cd backend" in text, "runbook 应统一 cd backend"
    assert "backend\\data\\notes.db" not in cmd_text and "backend/data/notes.db" not in cmd_text, \
        "runbook 命令中的数据库路径应写 data\\notes.db（相对 backend），不得写 backend\\data\\notes.db"

    # 6. Step 0 强制 $backupRoot 必须是尚不存在的新目录（已存在即拒绝复用），
    #    并同时安全创建 quarantine 子目录；恢复流程使用 $quarantineDir。
    assert re.search(r'Test-Path[^\n]*\$backupRoot[^\n]*\n\s*throw[^\n]*已存在[^\n]*拒绝复用', text) is not None, \
        "runbook 应在 backupRoot 已存在时拒绝复用（避免复用旧 baseline/backup-result）"
    assert '$quarantineDir = Join-Path $backupRoot "quarantine"' in text, \
        "runbook 应创建 quarantine 子目录（$quarantineDir）"
    assert 'New-Item -ItemType Directory -Path $quarantineDir -ErrorAction Stop' in text, \
        "runbook 应安全创建 $quarantineDir"
    assert '$quarantine = Join-Path $quarantineDir "notes.db.damaged-$ts"' in text, \
        "runbook 恢复流程应使用 $quarantineDir 构造 quarantine 路径"
