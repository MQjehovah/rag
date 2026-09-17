"""Alembic 迁移期兼容辅助（standalone，仅 stdlib + SQLAlchemy/Alembic）。

用途：Phase 9B 副本迁移兼容——真实库可能出现「alembic_version 停在旧 revision，
但部分后加对象（列/约束/索引/FK）已物理存在」的状态。

等价判断原则（本文件唯一事实来源）：
1. 缺失对象按原迁移定义创建。
2. 已存在且与目标定义**等价**的对象保留，不重复创建。
3. 列存在不代表迁移完成：缺失的 FK/CHECK/索引仍须补齐。
4. 同名/同关联对象**不等价**时受控失败（抛 RuntimeError）：
   不跳过、不静默改类型、不删列重建丢弃数据、不追加重复 FK/约束/索引。
5. 等价比较只做**安全归一**：
   - 列类型：去空白并大写（VARCHAR(N) vs VARCHAR ( N )）；nullable 必须一致；
   - server_default：不做大小写/去空格化（保留 SQL 字符串常量语义），仅两端同时为
     NULL/无默认视为等价；布尔 false/true 与 0/1 视为同一存储语义；
   - 索引：比较列序集合、unique、SQLite 部分索引谓词（无谓词与无谓词等价；
     一端有另一端无 → 不等价；谓词按字面量感知归一比较）；
   - FK：比较本地列、目标表/列与 ON DELETE 行为；同关联但 ON DELETE 不同 → 受控失败；
   - CHECK：用有界、引号感知、括号配平的提取逻辑，绝不用“截断到第一个右括号”的
     正则；比较仅安全归一（保留字符串常量大小写/空白）。
6. 新增约束前不自动清洗数据：SQLite batch 重建遇违反新约束的既有数据自然失败，
   数据保持（调用方按失败语义处理）。
7. 无名/他名但等价的既有 CHECK：为保证后续 downgrade（按名 drop）可用，迁移会补建
   一个**命名**等价约束（SQLite 允许并存同规则），并在结果注释里说明；不做“只让
   upgrade 暂时通过”的处理。
8. 不 import app 模型 / schema guard / main / worker；不触发外部调用。
9. 非 SQLite（PostgreSQL）分支：本模块不改动原迁移的非 sqlite 分支语义；其索引/FK
   若与既有同名对象冲突时按“无法可靠确认等价 → 受控失败”处理。PG 是否已实测见
   演练报告。
"""
from __future__ import annotations

import re

from alembic import op
from sqlalchemy import inspect

_RE_WS = re.compile(r"\s+")
_CHECK_START_RE = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
_CONSTRAINT_PREFIX_RE = re.compile(r"\bCONSTRAINT\s+([A-Za-z_][A-Za-z0-9_]*)\s*$",
                                   re.IGNORECASE)


def _norm(text) -> str:
    """列类型归一：去空白并大写（SQLAlchemy type 可能是对象）。"""
    if not isinstance(text, str):
        text = str(text or "")
    return _RE_WS.sub("", text).upper()


# ---------------------------------------------------------------------------
# 字面量感知归一（CHECK/索引谓词/默认值共用）
# ---------------------------------------------------------------------------


def _literal_aware_normalize(text: str, *, lower_keywords: bool) -> str:
    """把 text 规范成便于等价的 token 流。

    - 引号（' " ）内的内容原样保留（含大小写与空白语义）；
    - 引号外连续空白折叠为单个空格；
    - lower_keywords=True 时引号外内容小写（用于关键字比较）。
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    pending_space = False

    def flush_space() -> None:
        nonlocal pending_space
        if pending_space:
            out.append(" ")
            pending_space = False

    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == quote:
                # 处理 SQL 内转义：两个连续同引号视为字面中的一个引号。
                if i + 1 < n and text[i + 1] == quote:
                    out.append(quote)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            flush_space()
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch.isspace():
            pending_space = True
            i += 1
            continue
        flush_space()
        out.append(ch.lower() if lower_keywords else ch)
        i += 1
    return "".join(out).strip()


def _cond_norm(text: str) -> str:
    """CHECK/谓词条件的安全归一：引号外小写、空白折叠、字面量原样。"""
    return _literal_aware_normalize(text, lower_keywords=True)


def _scan_balanced_paren(text: str, open_idx: int):
    """从 text[open_idx]=='(' 起配平括号（引号感知），返回匹配 ')' 的索引或 None。"""
    depth = 0
    i = open_idx
    quote: str | None = None
    n = len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def extract_check_constraints(create_sql: str) -> list[tuple[str | None, str]]:
    """从 CREATE TABLE sql 中提取 (约束名或 None, 条件) 列表（有界、引号+括号配平）。

    只做有界扫描（上限保护），不做通用 SQL 解析。条件保留括号内的原始文本。
    """
    if not create_sql:
        return []
    cap = min(len(create_sql), 10 * 1024 * 1024)
    sql = create_sql[:cap]
    out: list[tuple[str | None, str]] = []
    for m in _CHECK_START_RE.finditer(sql):
        open_idx = m.end() - 1  # '('
        close = _scan_balanced_paren(sql, open_idx)
        if close is None:
            continue
        cond = sql[open_idx + 1:close]
        # 向前找紧邻的 CONSTRAINT <name>
        prefix = sql[max(0, m.start() - 96):m.start()]
        cm = _CONSTRAINT_PREFIX_RE.search(prefix)
        name = cm.group(1) if cm else None
        out.append((name, cond))
    return out


# ---------------------------------------------------------------------------
# 列：存在性 / 等价（含 server_default）
# ---------------------------------------------------------------------------


def existing_columns(bind, table: str) -> dict:
    insp = inspect(bind)
    if not insp.has_table(table):
        return {}
    return {c["name"]: c for c in insp.get_columns(table)}


def _boolean_default_canon(text: str | None) -> str | None:
    """把布尔类默认归一为 {'0','1',None}；无法判定返回原文。"""
    if text is None:
        return None
    t = str(text).strip().strip("'\"")
    low = t.lower()
    if low in ("true", "1"):
        return "1"
    if low in ("false", "0"):
        return "0"
    return t


def _server_default_equal(existing: dict, target) -> bool:
    """server_default 等价（不允许无依据差异）。

    - 两端均无默认（None）→ 等价；
    - 一端有另一端无 → 不等价；
    - 均有 → 若列类型为布尔（Boolean affinity）则按 {0/1/false/true} 归一再比较；
      否则按字面量感知归一（保留大小写/空白语义，lower_keywords=False）比较。
    """
    existing_default = existing.get("default")
    existing_text = str(existing_default) if existing_default is not None else None
    sd = getattr(target, "server_default", None)
    target_text = None
    if sd is not None:
        arg = getattr(sd, "arg", None)
        if arg is not None:
            if isinstance(arg, str):
                target_text = arg
            elif hasattr(arg, "text"):
                target_text = arg.text
            else:
                target_text = str(arg)
    if existing_text is None and target_text is None:
        return True
    if existing_text is None or target_text is None:
        return False
    # 布尔列：0/1/false/true 同一存储语义。
    from sqlalchemy import Boolean as _SaBoolean
    is_bool = isinstance(getattr(target, "type", None), _SaBoolean)
    if is_bool:
        return (_boolean_default_canon(existing_text)
                == _boolean_default_canon(target_text))
    return _literal_aware_normalize(existing_text, lower_keywords=False) == \
        _literal_aware_normalize(target_text, lower_keywords=False)


def _validate_existing_column(existing: dict, target, table: str) -> None:
    """已存在列与目标列等价校验；不等价抛受控错误。"""
    existing_type = _norm(existing.get("type") or "")
    target_type = _norm(str(target.type))
    if existing_type != target_type:
        raise RuntimeError(
            "migration_compat_type_mismatch: "
            f"{table}.{target.name} existing type={existing.get('type')!r} "
            f"vs target={target.type!r}"
        )
    existing_nullable = bool(existing.get("nullable"))
    target_nullable = bool(getattr(target, "nullable", True) is not False)
    if existing_nullable != target_nullable:
        raise RuntimeError(
            "migration_compat_nullable_mismatch: "
            f"{table}.{target.name} existing nullable={existing_nullable} "
            f"vs target nullable={target_nullable}"
        )
    if not _server_default_equal(existing, target):
        raise RuntimeError(
            "migration_compat_default_mismatch: "
            f"{table}.{target.name} existing default={existing.get('default')!r} "
            f"vs target server_default={getattr(target, 'server_default', None)!r}"
        )


def drop_columns_compat(op, table: str, columns) -> None:
    """删除列：SQLite 用 recreate='always'（无名 FK 只能重建表）；

    PostgreSQL 用 ALTER TABLE DROP COLUMN。PG 上 recreate=always 会先
    DROP PRIMARY KEY，若存在引用该 PK 的外键即失败——这是 SQLite 验收
    发现不了的方言缺陷。
    """
    names = list(columns)
    if not names:
        return
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(table, recreate="always") as batch:
            for name in names:
                batch.drop_column(name)
        return
    for name in names:
        op.drop_column(table, name)


def ensure_columns_sqlite(op, bind, table: str, columns) -> list:
    """SQLite：仅补齐缺失列；已存在列先做等价校验（含 server_default）。

    返回本次实际新增的列名。无缺失且全部等价时不做任何 DDL。
    """
    existing = existing_columns(bind, table)
    missing: list = []
    for col in columns:
        if col.name in existing:
            _validate_existing_column(existing[col.name], col, table)
        else:
            missing.append(col)
    if missing:
        with op.batch_alter_table(table) as batch_op:
            for col in missing:
                batch_op.add_column(col)
    return [c.name for c in missing]


# ---------------------------------------------------------------------------
# CHECK 约束
# ---------------------------------------------------------------------------


def table_sql(bind, table: str) -> str:
    res = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return (res[0] if res else "") or ""


def existing_checks(bind, table: str) -> list[tuple[str | None, str]]:
    return extract_check_constraints(table_sql(bind, table))


def ensure_checks_sqlite(op, bind, table: str, checks) -> list:
    """SQLite：补齐缺失的 CHECK 约束（batch 重建）。

    checks: [(constraint_name, condition), ...]。
    - 同名且条件等价 → 跳过；
    - 同名但条件不同 → 受控失败；
    - 仅存在无名/他名等价约束 → 补建命名等价约束（保证按名 downgrade/guard 可用），
      结果注释说明（升级不丢历史约束）；
    - 全部缺失 → 逐个创建命名约束。
    """
    existing = existing_columns(bind, table)
    if not existing:
        raise RuntimeError(
            f"migration_compat_missing_table: {table} does not exist"
        )
    have = existing_checks(bind, table)
    created: list[str] = []
    missing_named: list[tuple[str, str]] = []
    for name, condition in checks:
        norm = _cond_norm(condition)
        named = [e for e in have if e[0] is not None and _norm(e[0]) == _norm(name)]
        unnamed_equiv = [e for e in have
                         if (e[0] is None or _norm(e[0]) != _norm(name))
                         and _cond_norm(e[1]) == norm]
        if named:
            if any(_cond_norm(e[1]) == norm for e in named):
                continue  # 同名等价 → 复用
            raise RuntimeError(
                "migration_compat_constraint_conflict: "
                f"constraint {name!r} exists with different definition"
            )
        if unnamed_equiv:
            # 无名/他名等价存在：为 downgrade 兼容补一个命名等价约束。
            missing_named.append((name, condition))
            created.append(name)
            continue
        missing_named.append((name, condition))
        created.append(name)
    if missing_named:
        with op.batch_alter_table(table) as batch_op:
            for name, condition in missing_named:
                batch_op.create_check_constraint(name, condition)
    return created


# ---------------------------------------------------------------------------
# 索引（含 SQLite 部分索引谓词）
# ---------------------------------------------------------------------------


def _sqlite_indexes(bind, table: str) -> dict:
    """PRAGMA 直读索引：{name: {columns, unique, partial, where}}。"""
    rows = bind.exec_driver_sql(f"PRAGMA index_list({table})").fetchall()
    # 列序：seq, name, unique, origin, partial
    out: dict = {}
    for r in rows:
        name, unique, partial = r[1], r[2], r[4]
        cols: list[str] = []
        for x in bind.exec_driver_sql(f"PRAGMA index_xinfo({name})").fetchall():
            # seqno, cid, name, desc, coll, key（key=1 为主键/普通索引键列）
            if x[5] and x[2] is not None and x[1] >= 0:
                cols.append(x[2])
        sql_row = bind.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (name,),
        ).fetchone()
        sql_text = (sql_row[0] if sql_row else "") or ""
        where = None
        if partial:
            m = re.search(r"\)\s*WHERE\s+(.+)$", sql_text, re.IGNORECASE | re.DOTALL)
            where = m.group(1) if m else None
        out[name] = {"columns": cols, "unique": bool(unique),
                     "partial": bool(partial), "where": where}
    return out


def _indexes_equivalent(existing: dict, columns, unique: bool, where_sql) -> bool:
    if list(existing["columns"]) != list(columns):
        return False
    if bool(existing["unique"]) != bool(unique):
        return False
    e_where = existing.get("where")
    t_where = where_sql
    if (e_where is None) != (t_where is None):
        return False
    if e_where is not None and t_where is not None:
        return _cond_norm(e_where) == _cond_norm(t_where)
    return True


def ensure_index(op, bind, table: str, index_name: str, columns,
                 unique: bool = False, sqlite_where=None,
                 postgresql_where=None) -> bool:
    """补齐缺失索引；同名索引必须等价（列序/unique/部分谓词）否则受控失败。"""
    insp = inspect(bind)
    if not insp.has_table(table):
        raise RuntimeError(
            f"migration_compat_missing_table: {table} does not exist")
    dialect = bind.dialect.name
    if dialect == "sqlite":
        existing = _sqlite_indexes(bind, table)
    else:
        existing = {ix["name"]: {
            "columns": list(ix.get("column_names") or []),
            "unique": bool(ix.get("unique")),
            "partial": False,
            "where": None,
        } for ix in insp.get_indexes(table)}
    if index_name in existing:
        where = sqlite_where if dialect == "sqlite" else postgresql_where
        where_sql = str(where) if where is not None else None
        if not _indexes_equivalent(existing[index_name], columns, unique, where_sql):
            raise RuntimeError(
                "migration_compat_index_conflict: "
                f"index {index_name!r} on {table} exists with different definition "
                f"(existing={existing[index_name]!r}, target columns={list(columns)}, "
                f"unique={unique}, where={where_sql!r})"
            )
        return False
    op.create_index(
        index_name, table, columns, unique=unique,
        sqlite_where=sqlite_where, postgresql_where=postgresql_where,
    )
    return True


# ---------------------------------------------------------------------------
# 外键（本地列 / 目标表列 / ON DELETE 行为）
# ---------------------------------------------------------------------------


def existing_fks(bind, table: str) -> list:
    insp = inspect(bind)
    if not insp.has_table(table):
        return []
    return insp.get_foreign_keys(table)


def _fk_relation_status(existing: dict, local_cols, referent, remote_cols,
                        ondelete: str) -> str:
    """返回 'equal' | 'ondeletes_diff' | 'none'。"""
    same_cols = (
        list(existing.get("constrained_columns") or []) == list(local_cols)
        and existing.get("referred_table") == referent
        and list(existing.get("referred_columns") or []) == list(remote_cols)
    )
    if not same_cols:
        return "none"
    existing_ondelete = (existing.get("options") or {}).get("ondelete") or ""
    target_ondelete = (ondelete or "").strip()
    if (existing_ondelete or "").strip().upper() != target_ondelete.upper():
        return "ondeletes_diff"
    return "equal"


def ensure_fk_sqlite(op, bind, table: str, constraint_name: str, referent: str,
                     local_cols, remote_cols, ondelete: str) -> bool:
    """SQLite：补齐缺失 FK（等价判断含 ON DELETE）。

    - 已存在等价 FK（无论名称）→ 复用，不新建（sqlite downgrade 按列批删即可）；
    - 同关联但 ON DELETE 不同 → 受控失败（不追加重复 FK）；
    - 无关联 → 新建命名 FK。
    """
    fks = existing_fks(bind, table)
    for fk in fks:
        status = _fk_relation_status(fk, local_cols, referent, remote_cols, ondelete)
        if status == "equal":
            return False
        if status == "ondeletes_diff":
            raise RuntimeError(
                "migration_compat_fk_ondeletes_conflict: "
                f"FK on {table}({','.join(local_cols)}) -> {referent} "
                f"exists with ON DELETE {fk.get('options', {}).get('ondelete')!r} "
                f"vs target {ondelete!r}; not adding duplicate"
            )
    with op.batch_alter_table(table) as batch_op:
        batch_op.create_foreign_key(
            constraint_name, referent, list(local_cols), list(remote_cols),
            ondelete=ondelete,
        )
    return True
