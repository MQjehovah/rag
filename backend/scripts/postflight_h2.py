"""Phase H-2 postflight（只读，两个 stage）。

- reconciled：版本 a8b9c0d1e2f3，债务分类严格 migrated=1/no_scope=4/empty_query=1，
  三个 debt 索引 + 三个 users 索引定义完全正确，唯一约束 + CASCADE FK 正确，
  保留表行数与 baseline 一致。
- p33：版本 d1e2f3a4b5c6，21 张旧表和 3 个字段消失，8 个旧 flag 消失、有效 flag 保留，
  V4 表/索引/FK/关键行数不变，integrity/FK 全部通过。

任一不符非零退出，不得靠人工目测日志。

用法：
    python scripts/postflight_h2.py --db data/notes.db --stage reconciled
    python scripts/postflight_h2.py --db data/notes.db --stage p33
"""
from __future__ import annotations

import argparse
import json
import sys

from h2_schema import (
    DEBT_CLASSIFICATION_BASELINE,
    DEBT_INDEXES,
    DEBT_USERS_COLUMNS,
    DEBT_USERS_FK,
    DEBT_USERS_INDEXES,
    DROP_COLUMNS,
    DROP_TABLES,
    KEEP_FLAGS,
    KEEP_TABLES,
    LEGACY_FLAGS,
    P33_VERSION,
    RECONCILED_VERSION,
    ROW_BASELINE,
    USERS_COLUMNS,
    _readonly_conn,
    column_defs,
    debt_classification,
    flag_baseline,
    foreign_keys,
    has_cascade_fk,
    index_defs,
    table_names,
)


def _check(name, passed, detail):
    return {"check": name, "passed": bool(passed), "detail": detail}


def _postflight_reconciled(conn, baseline_flags=None) -> list[dict]:
    checks = []
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    version_val = version[0] if version else None
    checks.append(_check("版本 == a8b9c0d1e2f3", version_val == RECONCILED_VERSION, str(version_val)))

    # 债务分类
    cls = debt_classification(conn)
    cls_ok = cls == DEBT_CLASSIFICATION_BASELINE
    checks.append(_check(
        "债务分类 == migrated=1/no_scope=4/empty_query=1",
        cls_ok,
        json.dumps(cls, ensure_ascii=False),
    ))

    # 三个 debt 索引定义完全正确
    tables = table_names(conn)
    if "knowledge_debts" in tables:
        debt_idx = index_defs(conn, "knowledge_debts")
        for iname, (icols, iunique) in DEBT_INDEXES.items():
            if iname not in debt_idx:
                checks.append(_check(f"debt 索引 {iname} 存在", False, "缺失"))
                continue
            idx = debt_idx[iname]
            ok = idx["cols"] == icols and idx["unique"] is iunique
            checks.append(_check(f"debt 索引 {iname} 列/unique", ok, f"cols={idx['cols']} unique={idx['unique']}"))
    else:
        checks.append(_check("knowledge_debts 表存在", False, "缺失"))

    # 三个 users 索引定义完全正确
    if "knowledge_debt_users" in tables:
        users_idx = index_defs(conn, "knowledge_debt_users")
        for iname, (icols, iunique) in DEBT_USERS_INDEXES.items():
            if iname not in users_idx:
                checks.append(_check(f"users 索引 {iname} 存在", False, "缺失"))
                continue
            idx = users_idx[iname]
            ok = idx["cols"] == icols and idx["unique"] is iunique
            checks.append(_check(f"users 索引 {iname} 列/unique", ok, f"cols={idx['cols']} unique={idx['unique']}"))
        # CASCADE FK（结构化 foreign_key_list 判定，不依赖建表 SQL 字符串匹配）。
        fk_ok = has_cascade_fk(
            conn, "knowledge_debt_users",
            DEBT_USERS_FK["from"], DEBT_USERS_FK["table"], DEBT_USERS_FK["to"],
        )
        checks.append(_check(
            "debt_id → knowledge_debts.id ON DELETE CASCADE",
            fk_ok,
            "存在" if fk_ok else "缺失",
        ))
    else:
        checks.append(_check("knowledge_debt_users 表存在", False, "缺失"))

    # 保留表行数 == baseline
    for t, expected in ROW_BASELINE.items():
        if t in tables:
            actual = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            checks.append(_check(f"{t} 行数 == {expected}", actual == expected, f"实际 {actual}"))
        else:
            checks.append(_check(f"{t} 行数 == {expected}", False, "表缺失"))

    # Feature flag 状态必须与 preflight baseline 完全一致（P32.5 不改变 flag）。
    if baseline_flags is not None:
        current = flag_baseline(conn)
        unchanged = current == baseline_flags
        checks.append(_check(
            "feature flag 状态与 baseline 完全一致",
            unchanged,
            f"baseline={json.dumps(baseline_flags, ensure_ascii=False)} "
            f"current={json.dumps(current, ensure_ascii=False)}",
        ))

    return checks


def _postflight_p33(conn, baseline_flags=None) -> list[dict]:
    checks = []
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    version_val = version[0] if version else None
    checks.append(_check("版本 == d1e2f3a4b5c6", version_val == P33_VERSION, str(version_val)))

    tables = table_names(conn)

    # 21 张旧表消失
    remaining_drop = [t for t in DROP_TABLES if t in tables]
    checks.append(_check("21 张旧表消失", len(remaining_drop) == 0, f"残留 {remaining_drop}" if remaining_drop else "全部消失"))

    # 3 个待删字段消失
    missing_cols = []
    if "knowledge_debts" in tables:
        debt_cols = set(column_defs(conn, "knowledge_debts").keys())
        for col in DROP_COLUMNS.get("knowledge_debts", []):
            if col in debt_cols:
                missing_cols.append(f"knowledge_debts.{col}")
    if "pages" in tables:
        page_cols = set(column_defs(conn, "pages").keys())
        for col in DROP_COLUMNS.get("pages", []):
            if col in page_cols:
                missing_cols.append(f"pages.{col}")
    checks.append(_check("3 个待删字段消失", len(missing_cols) == 0, f"残留 {missing_cols}" if missing_cols else "全部消失"))

    # 8 个旧 flag 消失
    remaining_flags = []
    if "runtime_feature_flags" in tables:
        present = {r[0] for r in conn.execute("SELECT name FROM runtime_feature_flags").fetchall()}
        remaining_flags = [f for f in LEGACY_FLAGS if f in present]
    checks.append(_check("8 个旧 flag 消失", len(remaining_flags) == 0, f"残留 {remaining_flags}" if remaining_flags else "全部消失"))

    # 迁移前存在的所有非 legacy flag 及其 enabled 值完全不变（基于 preflight baseline）。
    if baseline_flags is not None:
        current = flag_baseline(conn)
        current_non_legacy = {k: v for k, v in current.items() if k not in LEGACY_FLAGS}
        baseline_non_legacy = {k: v for k, v in baseline_flags.items() if k not in LEGACY_FLAGS}
        unchanged = current_non_legacy == baseline_non_legacy
        checks.append(_check(
            "非 legacy flag 及 enabled 值与 baseline 完全一致",
            unchanged,
            f"baseline={json.dumps(baseline_non_legacy, ensure_ascii=False)} "
            f"current={json.dumps(current_non_legacy, ensure_ascii=False)}",
        ))

    # V4 表保留 + 关键行数不变
    missing_keep = [t for t in KEEP_TABLES if t not in tables]
    checks.append(_check("19 张保留表存在", len(missing_keep) == 0, f"缺 {missing_keep}" if missing_keep else "齐全"))
    for t, expected in ROW_BASELINE.items():
        if t in tables:
            actual = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            checks.append(_check(f"{t} 行数 == {expected}", actual == expected, f"实际 {actual}"))
        else:
            checks.append(_check(f"{t} 行数 == {expected}", False, "表缺失"))

    # 三个 debt 索引：名称/列顺序/unique 精确一致。
    if "knowledge_debts" in tables:
        debt_idx = index_defs(conn, "knowledge_debts")
        for iname, (icols, iunique) in DEBT_INDEXES.items():
            if iname not in debt_idx:
                checks.append(_check(f"debt 索引 {iname} 存在", False, "缺失"))
                continue
            idx = debt_idx[iname]
            ok = idx["cols"] == icols and idx["unique"] is iunique
            checks.append(_check(f"debt 索引 {iname} 列/unique", ok, f"cols={idx['cols']} unique={idx['unique']}"))
    else:
        checks.append(_check("knowledge_debts 表存在", False, "缺失"))

    # knowledge_debt_users 精确列集合/类型/nullable/PK + 三索引 + 结构化 FK。
    if "knowledge_debt_users" in tables:
        users_cols = column_defs(conn, "knowledge_debt_users")
        actual_user_cols = set(users_cols.keys())
        expected_user_cols = set(DEBT_USERS_COLUMNS.keys())
        checks.append(_check(
            "knowledge_debt_users 精确列集合",
            actual_user_cols == expected_user_cols,
            f"期望 {sorted(expected_user_cols)}，实际 {sorted(actual_user_cols)}",
        ))
        for name, (etype, nullable, pk) in DEBT_USERS_COLUMNS.items():
            if name not in users_cols:
                checks.append(_check(f"knowledge_debt_users.{name} 存在", False, "缺失"))
                continue
            col = users_cols[name]
            ok = (col["type"] or "").upper() == etype.upper() and col["notnull"] == (0 if nullable else 1) and col["pk"] == (1 if pk else 0)
            checks.append(_check(
                f"knowledge_debt_users.{name} 类型/nullable/PK",
                ok,
                f"type={col['type']!r} notnull={col['notnull']} pk={col['pk']}",
            ))

        users_idx = index_defs(conn, "knowledge_debt_users")
        for iname, (icols, iunique) in DEBT_USERS_INDEXES.items():
            if iname not in users_idx:
                checks.append(_check(f"users 索引 {iname} 存在", False, "缺失"))
                continue
            idx = users_idx[iname]
            ok = idx["cols"] == icols and idx["unique"] is iunique
            checks.append(_check(f"users 索引 {iname} 列/unique", ok, f"cols={idx['cols']} unique={idx['unique']}"))

        fk_ok = has_cascade_fk(
            conn, "knowledge_debt_users",
            DEBT_USERS_FK["from"], DEBT_USERS_FK["table"], DEBT_USERS_FK["to"],
        )
        checks.append(_check(
            "knowledge_debt_users.debt_id → knowledge_debts.id ON DELETE CASCADE",
            fk_ok,
            "存在" if fk_ok else "缺失",
        ))
    else:
        checks.append(_check("knowledge_debt_users 表存在", False, "缺失"))

    # users 精确列集合/类型/nullable/PK + 无多余外键。
    if "users" in tables:
        users_cols = column_defs(conn, "users")
        actual_user_cols = set(users_cols.keys())
        expected_user_cols = set(USERS_COLUMNS.keys())
        checks.append(_check(
            "users 精确列集合",
            actual_user_cols == expected_user_cols,
            f"期望 {sorted(expected_user_cols)}，实际 {sorted(actual_user_cols)}",
        ))
        for name, (etype, nullable, pk) in USERS_COLUMNS.items():
            if name not in users_cols:
                checks.append(_check(f"users.{name} 存在", False, "缺失"))
                continue
            col = users_cols[name]
            ok = (col["type"] or "").upper() == etype.upper() and col["notnull"] == (0 if nullable else 1) and col["pk"] == (1 if pk else 0)
            checks.append(_check(
                f"users.{name} 类型/nullable/PK",
                ok,
                f"type={col['type']!r} notnull={col['notnull']} pk={col['pk']}",
            ))
        checks.append(_check("users 无外键", len(foreign_keys(conn, "users")) == 0, str(foreign_keys(conn, "users"))))
    else:
        checks.append(_check("users 表存在", False, "缺失"))

    # integrity + FK
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    checks.append(_check("integrity_check == ok", integrity == "ok", str(integrity)))
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    checks.append(_check("foreign_key_check 通过", len(fk) == 0, f"{len(fk)} 违规"))

    return checks


def postflight(path: str, stage: str, baseline_flags: dict | None = None) -> dict:
    conn = _readonly_conn(path)
    try:
        if stage == "reconciled":
            checks = _postflight_reconciled(conn, baseline_flags)
        elif stage == "p33":
            checks = _postflight_p33(conn, baseline_flags)
        else:
            raise SystemExit(f"错误：未知 stage {stage}（可选 reconciled / p33）")
        all_ok = all(c["passed"] for c in checks)
        return {"db": path, "stage": stage, "passed": all_ok, "checks": checks}
    finally:
        conn.close()


def _load_baseline_flags(baseline_path: str) -> dict | None:
    """加载 preflight 生成的 baseline JSON，提取 fingerprint.flags（name→enabled）。"""
    if not baseline_path:
        return None
    with open(baseline_path, encoding="utf-8") as f:
        data = json.load(f)
    fp = data.get("fingerprint", data)
    flags = fp.get("flags")
    if not isinstance(flags, dict):
        raise SystemExit(f"错误：baseline 缺少 fingerprint.flags（name→enabled）：{baseline_path}")
    return flags


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase H-2 postflight（只读）")
    parser.add_argument("--db", required=True, help="数据库路径")
    parser.add_argument("--stage", required=True, choices=["reconciled", "p33"])
    parser.add_argument("--baseline", required=True, help="preflight 生成的 baseline JSON（验证 feature flag 不变，两个 stage 均必填）")
    parser.add_argument("--report", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args()

    baseline_flags = _load_baseline_flags(args.baseline)
    result = postflight(args.db, args.stage, baseline_flags)
    if args.report:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        for c in result["checks"]:
            mark = "PASS" if c["passed"] else "FAIL"
            print(f"[{mark}] {c['check']}: {c['detail']}")
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
