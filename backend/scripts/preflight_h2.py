"""Phase H-2 preflight（只读，绝不修改源库）。

校验目标库是否满足「已测试漂移形态」，全部通过才允许进入 stamp/upgrade：
- alembic_version 恰为 b8e9f0a1b2c3（P31）；
- P32 六字段精确类型/长度/nullable（漂移形态：列已由历史 init_db 补出）；
- knowledge_debt_users 精确列集合/类型/长度/nullable/PK/三索引/CASCADE FK；
- 三个 debt 索引恰好缺失（漂移形态，由 reconciliation 补齐）；
- 21 张待删表存在 + 3 个待删字段存在（符合预期）；
- 19 张保留表全部存在；
- 关键保留表行数 == 批准基线；
- knowledge_debts 总数 == 6（批准基线）；
- card_v3_enabled=1 存在（批准基线）；
- 无重复 cluster_key；integrity/FK 通过。

用法：
    python scripts/preflight_h2.py --db data/notes.db              # 只读 preflight
    python scripts/preflight_h2.py --db data/notes.db --baseline b.json  # 保存 baseline
    python scripts/preflight_h2.py --db data/restored.db --compare b.json # 对比 baseline
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from h2_schema import (
    DEBT_CLASSIFICATION_BASELINE,
    DEBT_COUNT_BASELINE,
    DEBT_INDEXES,
    DEBT_USERS_COLUMNS,
    DEBT_USERS_FK,
    DEBT_USERS_INDEXES,
    DROP_COLUMNS,
    DROP_TABLES,
    EXPECTED_VERSION,
    KEEP_TABLES,
    LEGACY_FLAGS,
    P32_COLUMNS,
    ROW_BASELINE,
    _readonly_conn,
    column_defs,
    debt_classification,
    dump_baseline,
    has_cascade_fk,
    index_defs,
    index_names,
    table_names,
)


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"check": name, "passed": bool(passed), "detail": detail}


def preflight(path: str) -> dict:
    conn = _readonly_conn(path)
    try:
        checks = []

        # 1. 版本
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        version_val = version[0] if version else None
        checks.append(_check("alembic_version == P31", version_val == EXPECTED_VERSION, str(version_val)))

        # 2. P32 六字段精确类型/长度/nullable
        tables = table_names(conn)
        if "knowledge_debts" in tables:
            debt_cols = column_defs(conn, "knowledge_debts")
            for name, expected_type in P32_COLUMNS.items():
                if name not in debt_cols:
                    checks.append(_check(f"knowledge_debts.{name} 存在", False, "缺失"))
                    continue
                col = debt_cols[name]
                type_ok = (col["type"] or "").upper() == expected_type.upper()
                nullable_ok = col["notnull"] == 0
                pk_ok = col["pk"] == 0
                checks.append(_check(
                    f"knowledge_debts.{name} 类型/长度/nullable",
                    type_ok and nullable_ok and pk_ok,
                    f"type={col['type']!r} notnull={col['notnull']} pk={col['pk']}",
                ))
        else:
            checks.append(_check("knowledge_debts 表存在", False, "缺失"))

        # 3. knowledge_debt_users 精确列/类型/长度/nullable/PK
        if "knowledge_debt_users" in tables:
            users_cols = column_defs(conn, "knowledge_debt_users")
            # 精确列集合
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

            # users 三索引精确定义
            users_idx = index_defs(conn, "knowledge_debt_users")
            for iname, (icols, iunique) in DEBT_USERS_INDEXES.items():
                if iname not in users_idx:
                    checks.append(_check(f"users 索引 {iname} 存在", False, "缺失"))
                    continue
                idx = users_idx[iname]
                ok = idx["cols"] == icols and idx["unique"] is iunique
                checks.append(_check(
                    f"users 索引 {iname} 列/unique",
                    ok,
                    f"cols={idx['cols']} unique={idx['unique']}",
                ))

            # CASCADE FK（结构化 foreign_key_list 判定，不依赖建表 SQL 字符串匹配）。
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

        # 4. 三个 debt 索引恰好缺失（漂移形态）
        if "knowledge_debts" in tables:
            debt_idx = index_defs(conn, "knowledge_debts")
            present = set(debt_idx.keys()) & set(DEBT_INDEXES.keys())
            checks.append(_check(
                "三个 debt 索引恰好缺失（漂移形态）",
                len(present) == 0,
                f"已存在 {sorted(present)}（应缺失）" if present else "恰好缺失",
            ))
        else:
            checks.append(_check("三个 debt 索引缺失", False, "knowledge_debts 缺失"))

        # 5. 21 张待删表存在 + 3 个待删字段存在
        missing_drop_tables = [t for t in DROP_TABLES if t not in tables]
        checks.append(_check("21 张待删表存在", len(missing_drop_tables) == 0, f"缺 {missing_drop_tables}" if missing_drop_tables else "齐全"))

        if "knowledge_debts" in tables and "pages" in tables:
            debt_drop_cols = set(column_defs(conn, "knowledge_debts").keys())
            page_drop_cols = set(column_defs(conn, "pages").keys())
            missing_drop_cols = []
            for col in DROP_COLUMNS.get("knowledge_debts", []):
                if col not in debt_drop_cols:
                    missing_drop_cols.append(f"knowledge_debts.{col}")
            for col in DROP_COLUMNS.get("pages", []):
                if col not in page_drop_cols:
                    missing_drop_cols.append(f"pages.{col}")
            checks.append(_check("3 个待删字段存在", len(missing_drop_cols) == 0, f"缺 {missing_drop_cols}" if missing_drop_cols else "齐全"))

        # 6. 19 张保留表存在
        missing_keep = [t for t in KEEP_TABLES if t not in tables]
        checks.append(_check("19 张保留表存在", len(missing_keep) == 0, f"缺 {missing_keep}" if missing_keep else "齐全"))

        # 7. 关键保留表行数基线
        for t, expected in ROW_BASELINE.items():
            if t in tables:
                actual = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                checks.append(_check(f"{t} 行数 == {expected}", actual == expected, f"实际 {actual}"))
            else:
                checks.append(_check(f"{t} 行数 == {expected}", False, "表缺失"))

        # 8. 债务总数 == 6
        debt_count = conn.execute("SELECT COUNT(*) FROM knowledge_debts").fetchone()[0] if "knowledge_debts" in tables else -1
        checks.append(_check(f"knowledge_debts 总数 == {DEBT_COUNT_BASELINE}", debt_count == DEBT_COUNT_BASELINE, f"实际 {debt_count}"))

        # 9. card_v3_enabled=1 存在
        card_flag = conn.execute(
            "SELECT COUNT(*) FROM runtime_feature_flags WHERE name='card_v3_enabled' AND enabled=1"
        ).fetchone()[0] if "runtime_feature_flags" in tables else 0
        checks.append(_check("card_v3_enabled=1 存在（批准基线）", card_flag == 1, f"匹配 {card_flag} 条"))

        # 10. 无重复 cluster_key
        dup = conn.execute(
            "SELECT COUNT(*) FROM (SELECT cluster_key FROM knowledge_debts "
            "WHERE cluster_key IS NOT NULL GROUP BY cluster_key HAVING COUNT(*) > 1)"
        ).fetchone()[0] if "knowledge_debts" in tables else -1
        checks.append(_check("无重复 cluster_key", dup == 0, f"{dup} 组重复"))

        # 11. integrity + FK
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        checks.append(_check("integrity_check == ok", integrity == "ok", str(integrity)))
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        checks.append(_check("foreign_key_check 通过", len(fk) == 0, f"{len(fk)} 违规"))

        all_ok = all(c["passed"] for c in checks)
        baseline = dump_baseline(conn, path)
        return {"db": path, "passed": all_ok, "checks": checks, "baseline": baseline}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase H-2 preflight（只读）")
    parser.add_argument("--db", required=True, help="数据库路径")
    parser.add_argument("--baseline", help="保存 baseline JSON 到指定路径")
    parser.add_argument("--compare", help="与已有 baseline JSON 对比")
    parser.add_argument("--report", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args()

    result = preflight(args.db)

    if args.compare:
        with open(args.compare, encoding="utf-8") as f:
            expected = json.load(f)
        actual = result["baseline"]["fingerprint"]
        expected_fp = expected.get("fingerprint", expected)
        # 对比 schema fingerprint（不含 db 路径）
        diff = []
        for key in expected_fp:
            if key not in actual:
                diff.append(f"缺 key {key}")
            elif actual[key] != expected_fp[key]:
                diff.append(f"key {key} 不一致")
        result["compare_diff"] = diff
        result["compare_passed"] = len(diff) == 0
        result["passed"] = result["passed"] and result["compare_passed"]

    if args.baseline:
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(result["baseline"], f, ensure_ascii=False, indent=2)

    if args.report:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        for c in result["checks"]:
            mark = "PASS" if c["passed"] else "FAIL"
            print(f"[{mark}] {c['check']}: {c['detail']}")
        if args.compare:
            mark = "PASS" if result.get("compare_passed") else "FAIL"
            print(f"[{mark}] baseline compare: {result.get('compare_diff', [])}")

    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
