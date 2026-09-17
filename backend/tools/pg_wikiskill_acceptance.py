"""WikiSkill 隔离 PostgreSQL/pgvector 验收工具。

仅在显式提供隔离 PG URL 时运行。不读取生产 .env，不连接默认 5432，
不自动对生产库执行迁移。默认 fail-closed。输出与异常均脱敏（不显示密码）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote

BACKEND = Path(__file__).resolve().parent.parent
EXPECTED_PORT = 55432
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
HEAD_REVISION = "4f83c9e2a1d7"
REQUIRED_DRIVER = "postgresql+psycopg2"

WIKISKILL_TABLES = (
    "evolution_skill_versions",
    "evolution_skill_bindings",
    "evolution_patterns",
    "evolution_pattern_revisions",
    "evolution_maintenance_runs",
    "evolution_logs",
    "evolution_indexes",
    "evolution_proposal_runs",
    "evolution_proposals",
    "evolution_experiments",
    "evolution_evaluations",
    "evolution_gate_events",
    "evolution_runs",
    "evolution_iterations",
    "evolution_business_events",
)
P52_BINDING_COLS = ("rev", "set_hash", "members_json")
P53_EVENT_COLS = (
    "from_members_json", "to_members_json",
    "from_set_hash", "to_set_hash", "from_rev", "to_rev",
)


class AcceptanceError(RuntimeError):
    def __init__(self, message: str, code: str = "acceptance_error"):
        super().__init__(message)
        self.code = code


def redact_url(url: str) -> str:
    """把 URL 中的密码替换为 ***，供日志/异常使用。"""
    if not url:
        return ""
    return re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", url)


def redact_text(text: str, url: str | None = None) -> str:
    out = text or ""
    if url:
        parsed = urlparse(url)
        password = unquote(parsed.password or "")
        if password:
            out = out.replace(password, "***")
        out = out.replace(url, redact_url(url))
    return redact_url(out)


def require_isolated_pg_url(url: str | None) -> str:
    """Fail-closed 校验隔离验收 URL。拒绝 SQLite / 默认 5432 / 远程 host。"""
    raw = (url or "").strip()
    if not raw:
        raise AcceptanceError(
            "未提供隔离 PostgreSQL URL（WIKISKILL_PG_ACCEPTANCE_URL 或 --url）",
            code="url_missing")
    lowered = raw.lower()
    if lowered.startswith("sqlite"):
        raise AcceptanceError("拒绝 SQLite：本工具只验收隔离 PostgreSQL",
                              code="sqlite_rejected")
    if not lowered.startswith(REQUIRED_DRIVER + "://"):
        raise AcceptanceError(
            f"数据库 URL 必须使用 {REQUIRED_DRIVER}://（已脱敏: {redact_url(raw)}）",
            code="driver_rejected")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise AcceptanceError(
            f"拒绝无法识别的远程 host（仅允许 127.0.0.1/localhost）: {host or '?'}",
            code="host_rejected")
    if parsed.port is None or int(parsed.port) != EXPECTED_PORT:
        raise AcceptanceError(
            f"端口必须是 {EXPECTED_PORT}（拒绝默认 5432 与其它端口）: "
            f"{parsed.port}",
            code="port_rejected")
    dbname = (parsed.path or "").lstrip("/").split("?")[0]
    token = dbname.lower()
    if "acceptance" not in token and "test" not in token:
        raise AcceptanceError(
            f"数据库名必须包含 acceptance 或 test: {dbname}",
            code="dbname_rejected")
    if not parsed.username:
        raise AcceptanceError("URL 缺少用户名", code="user_rejected")
    return raw


def _connect(url: str, *, dbname: str | None = None):
    import psycopg2
    parsed = urlparse(url)
    target = dbname if dbname is not None else (parsed.path or "").lstrip("/")
    try:
        return psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            dbname=target,
        )
    except Exception as exc:  # noqa: BLE001
        raise AcceptanceError(
            f"连接失败: {redact_text(str(exc), url)}", code="connect_failed"
        ) from None


def admin_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(path="/postgres").geturl()


def database_name(url: str) -> str:
    return (urlparse(url).path or "").lstrip("/").split("?")[0]


def replace_dbname(url: str, dbname: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(path="/" + dbname).geturl()


def sqlalchemy_engine(url: str, **kwargs):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool
    kwargs.setdefault("poolclass", NullPool)
    kwargs.setdefault("isolation_level", "READ COMMITTED")
    return create_engine(url, **kwargs)


def check_server(url: str) -> dict[str, Any]:
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute("SHOW server_version")
        version = cur.fetchone()[0]
        cur.execute("SELECT current_database(), current_user, inet_server_port()")
        db, user, port = cur.fetchone()
        cur.execute("SELECT version()")
        full = cur.fetchone()[0]
        return {
            "server_version": version,
            "full_version": full,
            "database": db,
            "user": user,
            "port": int(port),
            "ok": True,
        }
    finally:
        conn.close()


def check_pgvector(url: str) -> dict[str, Any]:
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        cur.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'")
        row = cur.fetchone()
        if not row:
            raise AcceptanceError("pgvector extension 创建后仍不可见",
                                  code="pgvector_missing")
        cur.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")
        dist = float(cur.fetchone()[0])
        return {"extension": "vector", "version": row[0],
                "distance_smoke": dist, "ok": True}
    finally:
        conn.close()


def check_transaction(url: str) -> dict[str, Any]:
    """用持久表 + 两个独立连接验证提交、回滚与读已提交隔离。"""
    table = "wikiskill_txn_smoke_accept"
    setup = _connect(url)
    setup.autocommit = True
    try:
        scur = setup.cursor()
        scur.execute(f'DROP TABLE IF EXISTS "{table}"')
        scur.execute(
            f'CREATE TABLE "{table}" (id int primary key, label text)')
    finally:
        setup.close()
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute(f'INSERT INTO "{table}" VALUES (1, %s)', ("committed",))
        conn.commit()
        cur.execute(f'INSERT INTO "{table}" VALUES (2, %s)', ("rolled-back",))
        conn.rollback()
        other = _connect(url)
        try:
            ocur = other.cursor()
            ocur.execute(f'SELECT id FROM "{table}" ORDER BY id')
            seen = [r[0] for r in ocur.fetchall()]
        finally:
            other.close()
        cur.execute(f'SELECT id FROM "{table}" ORDER BY id')
        mine = [r[0] for r in cur.fetchall()]
        ok = seen == [1] and mine == [1]
        return {"committed_rows": mine, "other_session_rows": seen,
                "rollback_ok": 2 not in mine, "isolation_ok": seen == [1],
                "ok": ok}
    finally:
        conn.close()
        cleanup = _connect(url)
        cleanup.autocommit = True
        try:
            cleanup.cursor().execute(f'DROP TABLE IF EXISTS "{table}"')
        finally:
            cleanup.close()


def create_database(url: str, dbname: str) -> None:
    if "acceptance" not in dbname.lower() and "test" not in dbname.lower():
        raise AcceptanceError(f"拒绝创建非验收库: {dbname}",
                              code="dbname_rejected")
    conn = _connect(url, dbname="postgres")
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,))
        if cur.fetchone():
            return
        cur.execute(f'CREATE DATABASE "{dbname}"')
    except Exception as exc:  # noqa: BLE001
        raise AcceptanceError(
            f"CREATE DATABASE 失败: {redact_text(str(exc), url)}",
            code="create_db_failed") from None
    finally:
        conn.close()


def drop_database(url: str, dbname: str) -> None:
    if "acceptance" not in dbname.lower() and "test" not in dbname.lower():
        raise AcceptanceError(f"拒绝删除非验收库: {dbname}",
                              code="dbname_rejected")
    if dbname == database_name(url) and dbname == "wikiskill_acceptance":
        # 主验收库由容器生命周期管理；测试可删派生库。
        raise AcceptanceError("拒绝删除主验收库 wikiskill_acceptance",
                              code="drop_primary_forbidden")
    conn = _connect(url, dbname="postgres")
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=%s AND pid <> pg_backend_pid()", (dbname,))
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    except Exception as exc:  # noqa: BLE001
        raise AcceptanceError(
            f"DROP DATABASE 失败: {redact_text(str(exc), url)}",
            code="drop_db_failed") from None
    finally:
        conn.close()


def alembic_upgrade(url: str, revision: str = "head", *,
                    cwd: Path | None = None) -> str:
    """对显式隔离 URL 执行 alembic upgrade。不读取生产 .env 作为连接依据。"""
    require_isolated_pg_url(url)
    env = os.environ.copy()
    # 覆盖任何继承来的 DATABASE_URL，避免 alembic 回退到生产配置。
    env["DATABASE_URL"] = url
    env.pop("ENV_FILE", None)
    cmd = [sys.executable, "-m", "alembic", "-x", f"database_url={url}",
           "upgrade", revision]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd or BACKEND), env=env,
            capture_output=True, text=True, check=False)
    except Exception as exc:  # noqa: BLE001
        raise AcceptanceError(
            f"alembic 启动失败: {redact_text(str(exc), url)}",
            code="alembic_failed") from None
    out = redact_text((proc.stdout or "") + "\n" + (proc.stderr or ""), url)
    if proc.returncode != 0:
        raise AcceptanceError(
            f"alembic upgrade {revision} 失败:\n{out}",
            code="alembic_failed")
    return out


def alembic_heads(cwd: Path | None = None) -> list[str]:
    env = os.environ.copy()
    # heads 只读脚本，仍避免把生产 URL 写进输出。
    env.pop("DATABASE_URL", None)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=str(cwd or BACKEND), env=env,
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AcceptanceError(
            f"alembic heads 失败: {redact_text(proc.stderr or proc.stdout or '')}",
            code="alembic_failed")
    heads = []
    for line in (proc.stdout or "").splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if re.fullmatch(r"[0-9a-f]+", token):
            heads.append(token)
    return heads


def current_revision(url: str) -> str | None:
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='alembic_version'")
        if cur.fetchone() is None:
            return None
        cur.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num")
        rows = cur.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise AcceptanceError(
                f"alembic_version 非单 head: {[r[0] for r in rows]}",
                code="multi_head")
        return rows[0][0]
    finally:
        conn.close()


def table_columns(url: str, table: str) -> set[str]:
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,))
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def existing_tables(url: str) -> set[str]:
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public'")
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def table_indexes(url: str, table: str) -> set[str]:
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname='public' AND tablename=%s", (table,))
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def table_constraints(url: str, table: str) -> set[str]:
    conn = _connect(url)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema='public' AND table_name=%s", (table,))
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def verify_wikiskill_schema(url: str) -> dict[str, Any]:
    tables = existing_tables(url)
    missing = [t for t in WIKISKILL_TABLES if t not in tables]
    binding_cols = table_columns(url, "evolution_skill_bindings")
    event_cols = table_columns(url, "evolution_business_events")
    missing_p52 = [c for c in P52_BINDING_COLS if c not in binding_cols]
    missing_p53 = [c for c in P53_EVENT_COLS if c not in event_cols]
    indexes = table_indexes(url, "evolution_skill_bindings")
    constraints = table_constraints(url, "evolution_skill_bindings")
    constraints |= table_constraints(url, "evolution_business_events")
    constraints |= table_constraints(url, "evolution_skill_versions")
    ok = (not missing and not missing_p52 and not missing_p53
          and "ux_evolution_binding_scope" in (indexes | constraints)
          and "ux_evolution_business_event_idem" in (
              table_indexes(url, "evolution_business_events") | constraints)
          and "ck_evolution_binding_pair" in constraints)
    return {
        "missing_tables": missing,
        "missing_p52_columns": missing_p52,
        "missing_p53_columns": missing_p53,
        "binding_indexes": sorted(indexes),
        "ok": ok,
    }


def pg_dump_custom(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    env = os.environ.copy()
    env["PGPASSWORD"] = unquote(parsed.password or "")
    cmd = [
        "docker", "exec", "-e", f"PGPASSWORD={env['PGPASSWORD']}",
        "wikiskill-pg-acceptance-20260908",
        "pg_dump", "-U", unquote(parsed.username or ""),
        "-d", database_name(url), "-Fc",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except Exception as exc:  # noqa: BLE001
        raise AcceptanceError(
            f"pg_dump 启动失败: {redact_text(str(exc), url)}",
            code="dump_failed") from None
    if proc.returncode != 0:
        err = redact_text((proc.stderr or b"").decode("utf-8", "replace"), url)
        raise AcceptanceError(f"pg_dump 失败: {err}", code="dump_failed")
    dest.write_bytes(proc.stdout)
    return dest


def pg_restore_custom(url: str, dump_path: Path, dest_db: str) -> None:
    create_database(url, dest_db)
    parsed = urlparse(url)
    password = unquote(parsed.password or "")
    # 把备份送进容器再 restore，避免在宿主机依赖 pg_restore。
    container = "wikiskill-pg-acceptance-20260908"
    remote = f"/tmp/{dump_path.name}"
    cp = subprocess.run(
        ["docker", "cp", str(dump_path), f"{container}:{remote}"],
        capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        raise AcceptanceError(
            f"docker cp 失败: {redact_text(cp.stderr or cp.stdout or '', url)}",
            code="restore_failed")
    proc = subprocess.run(
        ["docker", "exec", "-e", f"PGPASSWORD={password}", container,
         "pg_restore", "-U", unquote(parsed.username or ""),
         "-d", dest_db, "--no-owner", "--no-acl", remote],
        capture_output=True, text=True, check=False)
    subprocess.run(
        ["docker", "exec", container, "rm", "-f", remote],
        capture_output=True, check=False)
    if proc.returncode != 0:
        raise AcceptanceError(
            f"pg_restore 失败: {redact_text(proc.stderr or proc.stdout or '', url)}",
            code="restore_failed")


def _print(payload: dict) -> None:
    import json
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_check(url: str) -> int:
    info = check_server(url)
    vec = check_pgvector(url)
    txn = check_transaction(url)
    heads = alembic_heads()
    _print({
        "mode": "check",
        "url": redact_url(url),
        "server": {k: info[k] for k in (
            "server_version", "database", "user", "port", "ok")},
        "pgvector": vec,
        "transaction": txn,
        "alembic_heads": heads,
        "single_head": heads == [HEAD_REVISION],
        "production_env_loaded": False,
        "ok": bool(info["ok"] and vec["ok"] and txn["ok"]
                   and heads == [HEAD_REVISION]),
    })
    if not (info["ok"] and vec["ok"] and txn["ok"]
            and heads == [HEAD_REVISION]):
        return 1
    return 0


def cmd_run(url: str) -> int:
    # --run：健康检查 + 空库 upgrade head（仅隔离 URL）。不碰生产。
    rc = cmd_check(url)
    if rc != 0:
        return rc
    current = current_revision(url)
    if current is None:
        alembic_upgrade(url, "head")
        current = current_revision(url)
    schema = verify_wikiskill_schema(url)
    _print({
        "mode": "run",
        "url": redact_url(url),
        "alembic_version": current,
        "schema": schema,
        "ok": current == HEAD_REVISION and schema["ok"],
    })
    return 0 if current == HEAD_REVISION and schema["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="WikiSkill 隔离 PostgreSQL 验收（fail-closed）")
    parser.add_argument("--check", action="store_true",
                        help="只做连接/pgvector/事务/单 head 检查")
    parser.add_argument("--run", action="store_true",
                        help="检查后对隔离 URL 执行 alembic upgrade head")
    parser.add_argument("--url", default=os.environ.get(
        "WIKISKILL_PG_ACCEPTANCE_URL", ""),
                        help="隔离 PostgreSQL URL（也可设环境变量）")
    args = parser.parse_args(argv)
    if not args.check and not args.run:
        print("fail-closed: 必须显式指定 --check 或 --run", file=sys.stderr)
        return 2
    if args.check and args.run:
        print("fail-closed: --check 与 --run 互斥", file=sys.stderr)
        return 2
    try:
        url = require_isolated_pg_url(args.url)
        return cmd_run(url) if args.run else cmd_check(url)
    except AcceptanceError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print("acceptance_error: " + redact_text(str(exc), args.url),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
