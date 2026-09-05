"""schema-aware 只读盘点（inventory）。

只通过 guard.readonly_conn 的 `mode=ro` 连接读取，绝不写库。所有表/列/范围判定
都做存在性防御：缺失的表/列输出字符串 "unavailable" 而不是崩溃。

同时导出一批核心只读函数（quick_check / foreign_key_check / 行数 / 表与列探测 /
notebook 范围分类），供 postflight / recovery 复用。

输出约束：不输出正文 / 凭证 / 完整 id（id/样本一律用 sha256[:12] 脱敏）。

Notebook ACL 范围分类语义与 app/core/access_control.py 的 scope_from_notebook
保持一致（此处为 sqlite 纯 SQL 只读镜像，真实写路径必须复用 production 函数，
见 backfill.py）。判定规则（对齐 access_control.py:186-217）：
- 无任何组 → company；
- 仅 __public__ → company；
- 含管理员组 → admin；
- 仅业务组 → group；
- 混合 __public__ 与业务组 → unknown（fail closed）。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from phase9b_migration import guard

COMPANY = "company"
ADMIN = "admin"
GROUP = "group"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"

# 需要盘点/计数/一致性核对的核心表。
CORE_TABLES = [
    "notebooks",
    "notebook_groups",
    "pages",
    "page_chunks",
    "wiki_pages",
    "wiki_revisions",
    "wiki_sections",
    "wiki_workspaces",
    "notebook_workspace_bindings",
    "evidence_items",
    "wiki_section_evidence_bindings",
    "v4_graph_entities",
    "v4_graph_relations",
    "v4_graph_relation_evidence",
    "v4_graph_entity_evidence",
    "v4_graph_communities",
]

# 图谱相关表（Wiki/Page/Revision/Section/Evidence/图谱 计数用）。
GRAPH_TABLES = [
    "v4_graph_entities",
    "v4_graph_relations",
    "v4_graph_relation_evidence",
    "v4_graph_entity_evidence",
    "v4_graph_communities",
]

# 关键列存在性检查。
KEY_COLUMNS = {
    "notebooks": ["id", "name", "group_id"],
    "notebook_groups": ["notebook_id", "group_name"],
    "pages": ["id", "notebook_id", "content", "index_dirty", "wiki_dirty"],
    "wiki_pages": ["id", "title", "workspace_id", "status", "dirty", "acl_scope"],
    "wiki_workspaces": ["id", "key", "acl_scope", "scope_id", "status"],
    "notebook_workspace_bindings": ["id", "notebook_id", "workspace_id", "status"],
    "wiki_revisions": ["id", "wiki_page_id", "status"],
    "wiki_sections": ["id", "revision_id", "section_type", "heading"],
    "evidence_items": ["id", "content", "content_hash", "status"],
    "wiki_section_evidence_bindings": ["id", "section_id", "evidence_id", "field_path"],
}

_PUBLIC_MARKERS = {"__public__"}


# ---------------------------------------------------------------------------
# 核心只读 helper（export：postflight/recovery 复用）
# ---------------------------------------------------------------------------

def open_readonly(db_path):
    """上下文管理器：只读打开 db_path，自动关闭。"""
    @contextmanager
    def _open():
        conn = guard.readonly_conn(db_path)
        try:
            yield conn
        finally:
            conn.close()
    return _open()


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    """返回表列名；表不存在返回 []（不抛错）。"""
    if not table_exists(conn, table):
        return []
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def table_row_count(conn: sqlite3.Connection, table: str):
    """表行数；表缺失返回 None（不抛错）。"""
    if not table_exists(conn, table):
        return None
    return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def quick_check_rows(conn: sqlite3.Connection) -> list[str]:
    """PRAGMA quick_check：仅返回异常行（全 ok 时返回 []）。

    SQLite 的 quick_check 正常时也返回一行 ('ok',)，因此需过滤。
    """
    rows = conn.execute("PRAGMA quick_check").fetchall()
    return [r[0] for r in rows if r and r[0] and r[0].strip().lower() != "ok"]


def foreign_key_rows(conn: sqlite3.Connection) -> list[dict]:
    """PRAGMA foreign_key_check：外键违规明细（[] = 通过）。

    注意：SQLite 仅在开启外键时级联生效，但本检查独立于该开关，永远可用。
    """
    cols = ["table", "rowid", "parent", "fkid"]
    return [dict(zip(cols, r)) for r in conn.execute("PRAGMA foreign_key_check").fetchall()]


def run_quick_check(db_path: str) -> dict:
    with open_readonly(db_path) as conn:
        rows = quick_check_rows(conn)
    return {"passed": not rows, "rows": rows}


def run_fk_check(db_path: str) -> dict:
    with open_readonly(db_path) as conn:
        rows = foreign_key_rows(conn)
    return {"passed": not rows, "rows": rows}


def read_only_counts(db_path: str) -> dict:
    """CORE_TABLES 行数快照（缺失表为 None），供 recovery 前后对照。"""
    out = {}
    with open_readonly(db_path) as conn:
        for t in CORE_TABLES:
            out[t] = table_row_count(conn, t)
    return out


# ---------------------------------------------------------------------------
# Notebook ACL 范围分类（只读镜像 access_control.scope_from_notebook 语义）
# ---------------------------------------------------------------------------

def configured_admin_groups() -> set[str]:
    """读取 production 配置的管理员组（本地试点组 + LDAP 配置），纯调用。"""
    from app.core.access_control import _configured_admin_groups
    return set(_configured_admin_groups())


def classify_notebook_scope(
    group_id,
    extra_groups=(),
    admin_groups=("__local_admin__",),
) -> str:
    """镜像 access_control.scope_from_notebook：返回 company/admin/group/unknown。

    参数：notebooks.group_id；notebook_groups 表额外业务组；管理员组集合。
    """
    ag = set(admin_groups or ())
    groups: set[str] = set()
    if group_id is not None and str(group_id).strip():
        groups.add(str(group_id).strip())
    for g in extra_groups or ():
        if g is not None and str(g).strip():
            groups.add(str(g).strip())
    if not groups:
        return COMPANY
    if groups <= _PUBLIC_MARKERS:
        return COMPANY
    if groups & ag:
        return ADMIN
    business = groups - _PUBLIC_MARKERS - ag
    if not business:
        return UNKNOWN
    if groups & _PUBLIC_MARKERS:
        return UNKNOWN  # 混合 __public__ 与业务组 → fail closed
    return GROUP


def canonical_scope_key(kind: str, groups) -> str:
    """规范化同 ACL 分组键：company / admin / group:<sorted> / unknown。"""
    if kind == GROUP:
        return f"{GROUP}:{','.join(sorted(groups))}"
    return kind


def short_hash(identifier) -> str:
    """id 脱敏：sha256(utf-8)[:12]。"""
    return hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 主盘点
# ---------------------------------------------------------------------------

def _flag_dist(conn: sqlite3.Connection, table: str, column: str):
    """布尔列 1/0/null 分布；表/列缺失返回 "unavailable"。"""
    if column not in set(column_names(conn, table)):
        return UNAVAILABLE
    dist = {"1": 0, "0": 0, "null": 0}
    for (v,) in conn.execute(f'SELECT "{column}" FROM "{table}"'):
        if v is None:
            dist["null"] += 1
        elif v:
            dist["1"] += 1
        else:
            dist["0"] += 1
    return dist


def notebook_dataset(conn: sqlite3.Connection):
    """读取 notebooks / notebook_groups / bindings / workspaces 的只读数据集。

    返回 dict；对应表/关键列缺失的键为 None（不抛错）。
    """
    nb_cols = set(column_names(conn, "notebooks"))
    has_nb = table_exists(conn, "notebooks") and {"id", "name", "group_id"} <= nb_cols
    notebooks = None
    if has_nb:
        notebooks = [
            {"id": r[0], "name": r[1], "group_id": r[2]}
            for r in conn.execute("SELECT id, name, group_id FROM notebooks").fetchall()
        ]

    group_map = None
    if table_exists(conn, "notebook_groups") and {
        "notebook_id", "group_name"
    } <= set(column_names(conn, "notebook_groups")):
        group_map = {}
        for nb_id, gn in conn.execute(
            "SELECT notebook_id, group_name FROM notebook_groups"
        ).fetchall():
            if gn is not None and str(gn).strip():
                group_map.setdefault(nb_id, set()).add(str(gn).strip())

    bindings = None
    if table_exists(conn, "notebook_workspace_bindings") and {
        "notebook_id", "workspace_id", "status"
    } <= set(column_names(conn, "notebook_workspace_bindings")):
        bindings = [
            {"notebook_id": r[0], "workspace_id": r[1], "status": r[2]}
            for r in conn.execute(
                "SELECT notebook_id, workspace_id, status "
                "FROM notebook_workspace_bindings"
            ).fetchall()
        ]

    workspaces = None
    if table_exists(conn, "wiki_workspaces") and {
        "id", "key", "status"
    } <= set(column_names(conn, "wiki_workspaces")):
        workspaces = {
            r[0]: {"key": r[1], "status": r[2]}
            for r in conn.execute(
                "SELECT id, key, status FROM wiki_workspaces"
            ).fetchall()
        }

    return {
        "notebooks": notebooks,
        "group_map": group_map or {},
        "bindings": bindings or [],
        "workspaces": workspaces or {},
        "has_scope_col": has_nb,
    }


def _backfill_classification(nb: dict, ds: dict, ws_key_of, admin_groups: set[str]):
    """单个 notebook 的回填决策（镜像 routing.ensure_notebook_workspace，见文件头说明）。

    决策优先序（对齐 backend/app/core/wiki_workspace/routing.py:89-142）：
    1) 有 active binding → active；
    2) 无 active 但存在任一 binding（disabled 残留）→ blocked_disabled；
    3) 完全无 binding 且 scope UNKNOWN → blocked_unknown；
    4) 完全无 binding 且默认 key workspace 已 archived → blocked_archived；
    5) 否则 → candidate（可创建默认 workspace + active binding）。
    """
    nb_id = nb["id"]
    b_for = [b for b in ds["bindings"] if b["notebook_id"] == nb_id]
    if any(b["status"] == "active" for b in b_for):
        return {"decision": "active", "scope_kind": None, "scope_id": None}
    if b_for:
        return {"decision": "blocked_disabled", "scope_kind": None, "scope_id": None}
    kind = classify_notebook_scope(
        nb.get("group_id"), ds["group_map"].get(nb_id, ()), admin_groups
    )
    scope_id = None if kind == UNKNOWN else kind
    if kind == GROUP:
        extra = ds["group_map"].get(nb_id, set())
        scope_id = f"{GROUP}:{','.join(sorted(extra))}"
    if kind == UNKNOWN:
        return {"decision": "blocked_unknown", "scope_kind": UNKNOWN, "scope_id": UNKNOWN}
    key = ws_key_of(nb_id)
    ws = next((w for w in ds["workspaces"].values() if w["key"] == key), None)
    if ws is not None and ws["status"] != "active":
        return {
            "decision": "blocked_archived", "scope_kind": kind, "scope_id": scope_id,
        }
    return {"decision": "candidate", "scope_kind": kind, "scope_id": scope_id}


def inventory(db_path, md_out=None) -> dict:
    """只读盘点真实库副本/工作库，返回结构化结果；可选写 markdown 报告。

    绝不写库：内部只用 mode=ro 连接。表/列缺失输出 "unavailable"。
    """
    db_path = os.path.abspath(str(db_path))
    guard.validate_db_file(db_path)
    result: dict = {
        "db_path": db_path,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "readonly": True,
    }

    with open_readonly(db_path) as conn:
        present = table_names(conn)
        # 表存在性 + 行数
        tables = {}
        missing = []
        for t in CORE_TABLES:
            if t in present:
                tables[t] = {"exists": True, "rows": table_row_count(conn, t)}
            else:
                tables[t] = {"exists": False, "rows": UNAVAILABLE}
                missing.append(t)
        result["tables"] = tables
        result["missing_tables"] = missing

        # 关键列存在性
        columns = {}
        for t, cols in KEY_COLUMNS.items():
            has = set(column_names(conn, t))
            for c in cols:
                columns[f"{t}.{c}"] = c in has if t in present else UNAVAILABLE
        result["columns"] = columns

        # 版本占位（head 真校验由 preflight/upgrade 执行）
        try:
            from phase9b_migration import migrate as _migrate
            current = _migrate.current_version(db_path)
        except Exception:
            current = UNAVAILABLE
        result["version"] = {
            "sqlite_library": sqlite3.sqlite_version,
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "alembic_head_expected": _migrate_expected_head(),
            "alembic_current": current if current is not None else "none",
            "note": "head 校验占位：真实 head 校验在 preflight/upgrade 环节执行",
        }

        # 分类统计计数
        counts = {}
        has = lambda t: t in present  # noqa: E731
        counts["notebooks"] = tables["notebooks"]["rows"]
        counts["pages"] = tables["pages"]["rows"]
        counts["page_chunks"] = tables["page_chunks"]["rows"]
        counts["wiki_pages"] = tables["wiki_pages"]["rows"]
        counts["wiki_revisions"] = tables["wiki_revisions"]["rows"]
        counts["wiki_sections"] = tables["wiki_sections"]["rows"]
        counts["evidence_items"] = tables["evidence_items"]["rows"]
        counts["workspaces"] = tables["wiki_workspaces"]["rows"]
        counts["bindings"] = tables["notebook_workspace_bindings"]["rows"]
        counts["section_evidence_bindings"] = tables["wiki_section_evidence_bindings"]["rows"]
        for gt in GRAPH_TABLES:
            counts[f"graph_{gt.split('_', 2)[-1]}"] = tables[gt]["rows"]
        result["counts"] = counts

        # dirty / index_dirty 分布
        result["dirty_distribution"] = {
            "pages_index_dirty": _flag_dist(conn, "pages", "index_dirty"),
            "pages_wiki_dirty": _flag_dist(conn, "pages", "wiki_dirty"),
            "wiki_pages_dirty": _flag_dist(conn, "wiki_pages", "dirty"),
        }

        # ---- Notebook ACL 分布 + 回填候选/blocked + 同 ACL ----
        ds = notebook_dataset(conn)
        if ds["notebooks"] is None:
            result["acl_distribution"] = UNAVAILABLE
            result["backfill"] = {
                "unavailable": True,
                "reason": "notebooks 表或其 id/name/group_id 列缺失",
            }
            result["same_acl"] = UNAVAILABLE
        else:
            admin_groups = configured_admin_groups()
            from app.core.access_control import workspace_key_for_notebook as _ws_key

            acl_dist = {COMPANY: 0, ADMIN: 0, GROUP: 0, UNKNOWN: 0}
            missing_group = 0
            scope_ids = {}  # notebook_id -> canonical scope key
            backfill_counts = {
                "candidate": 0, "active": 0, "blocked_disabled": 0,
                "blocked_unknown": 0, "blocked_archived": 0,
            }
            for nb in ds["notebooks"]:
                nb_id = nb["id"]
                extra = ds["group_map"].get(nb_id, set())
                kind = classify_notebook_scope(nb.get("group_id"), extra, admin_groups)
                acl_dist[kind] += 1
                if not str(nb.get("group_id") or "").strip() and not extra:
                    missing_group += 1
                gs_all = set(extra)
                if nb.get("group_id") and str(nb.get("group_id")).strip():
                    gs_all.add(str(nb.get("group_id")).strip())
                if kind == GROUP:
                    business = gs_all - _PUBLIC_MARKERS - set(admin_groups)
                    scope_ids[nb_id] = f"{GROUP}:{','.join(sorted(business))}"
                elif kind == COMPANY or kind == ADMIN:
                    scope_ids[nb_id] = kind
                else:
                    scope_ids[nb_id] = UNKNOWN
                dec = _backfill_classification(nb, ds, _ws_key, admin_groups)
                backfill_counts[dec["decision"]] += 1
            result["acl_distribution"] = {
                "company": acl_dist[COMPANY],
                "admin": acl_dist[ADMIN],
                "group": acl_dist[GROUP],
                "unknown": acl_dist[UNKNOWN],
                "missing_group": missing_group,
                "total": len(ds["notebooks"]),
            }
            result["backfill"] = {
                "unavailable": False,
                "candidate": backfill_counts["candidate"],   # 回填候选（无 active binding 且可建）
                "already_active": backfill_counts["active"],
                "blocked_disabled": backfill_counts["blocked_disabled"],
                "blocked_unknown": backfill_counts["blocked_unknown"],
                "blocked_archived_default": backfill_counts["blocked_archived"],
                "note": "decision 规则镜像 routing.ensure_notebook_workspace",
            }
            # 同 ACL 不同 Notebook 数：规范 scope 键内 ≥2 个 notebook 的组。
            from collections import Counter
            cnt = Counter(v for v in scope_ids.values() if v and v != UNKNOWN)
            shared = {k: n for k, n in cnt.items() if n >= 2}
            result["same_acl"] = {
                "distinct_shared_acl_scopes": len(shared),
                "notebooks_in_shared_acl": sum(shared.values()),
                "shared_acl_samples": {
                    k: short_hash(k) for k in sorted(shared)[:5]
                },
            }

        # 同名 Wiki 冲突候选：按 trim(title) 分组，跨 >1 workspace 的标题数。
        # NULL workspace 归为 "__none__"（伪 workspace），避免无归属行被误判为冲突。
        if has("wiki_pages") and {"id", "title", "workspace_id"} <= set(
            column_names(conn, "wiki_pages")
        ):
            from collections import defaultdict
            title_ws: dict = defaultdict(set)
            title_ids: dict = defaultdict(list)
            for wid, wsid, title in conn.execute(
                "SELECT id, workspace_id, trim(title) FROM wiki_pages "
                "WHERE title IS NOT NULL AND length(trim(title)) > 0"
            ).fetchall():
                bucket = wsid if wsid is not None else "__none__"
                title_ws[title].add(bucket)
                title_ids[title].append(wid)
            conflicts = {t: s for t, s in title_ws.items() if len(s) > 1}
            conflict_rows = sum(len(title_ids[t]) for t in conflicts)
            result["wiki_name_conflicts"] = {
                "conflict_titles": len(conflicts),
                "conflict_wiki_rows": conflict_rows,
                "sample_title_hashes": [short_hash(t) for t in sorted(conflicts)[:5]],
            }
        else:
            result["wiki_name_conflicts"] = UNAVAILABLE

        # orphan 候选
        orphans = {"bindings_without_notebook": None,
                   "bindings_without_workspace": None,
                   "wiki_without_workspace": None,
                   "unused_active_workspaces": None}
        if ds["bindings"]:
            nb_ids = {nb["id"] for nb in (ds["notebooks"] or [])}
            orphans["bindings_without_notebook"] = sum(
                1 for b in ds["bindings"] if b["notebook_id"] not in nb_ids
            )
            orphans["bindings_without_workspace"] = sum(
                1 for b in ds["bindings"] if b["workspace_id"] not in ds["workspaces"]
            )
        if has("wiki_pages") and "workspace_id" in set(
            column_names(conn, "wiki_pages")
        ):
            ws_ids = set(ds["workspaces"]) if ds["workspaces"] else set()
            all_ws = {r[0] for r in conn.execute("SELECT id FROM wiki_workspaces")}
            orphans["wiki_without_workspace"] = conn.execute(
                "SELECT COUNT(*) FROM wiki_pages WHERE workspace_id IS NULL"
            ).fetchone()[0]
            orphans["wiki_bad_workspace_ref"] = conn.execute(
                "SELECT COUNT(*) FROM wiki_pages WHERE workspace_id IS NOT NULL "
                "AND workspace_id NOT IN (SELECT id FROM wiki_workspaces)"
            ).fetchone()[0]
        if ds["workspaces"]:
            bound_ws = {b["workspace_id"] for b in ds["bindings"]}
            orphan_ws = set()
            if has("wiki_pages") and "workspace_id" in set(
                column_names(conn, "wiki_pages")
            ):
                used_ws = {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT workspace_id FROM wiki_pages "
                        "WHERE workspace_id IS NOT NULL"
                    ).fetchall()
                }
            else:
                used_ws = set()
            for ws_id, w in ds["workspaces"].items():
                if w["status"] == "active" and ws_id not in bound_ws and ws_id not in used_ws:
                    orphan_ws.add(ws_id)
            orphans["unused_active_workspaces"] = len(orphan_ws)
        result["orphans"] = orphans

    if md_out:
        result["report_path"] = _write_markdown(result, md_out)
    return result


def _migrate_expected_head() -> str:
    from phase9b_migration import migrate as _migrate
    return _migrate.ALEMBIC_HEAD_EXPECTED


def _write_markdown(result: dict, md_out) -> str:
    """把 inventory 结果写成人类可读的 markdown 报告（UTF-8 无 BOM）。"""
    path = os.path.abspath(str(md_out))
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    lines: list[str] = []
    lines.append("# Phase 9B 只读盘点报告")
    lines.append("")
    lines.append(f"- 数据库：`{result['db_path']}`")
    lines.append(f"- 生成时间：{result['generated_at']}")
    v = result["version"]
    lines.append(
        f"- 版本：sqlite {v['sqlite_library']} / user_version={v['user_version']} / "
        f"alembic={v['alembic_current']}（预期 head={v['alembic_head_expected']}，校验在 preflight）"
    )
    lines.append("")
    lines.append("## 计数")
    lines.append("")
    for k, val in result["counts"].items():
        lines.append(f"- {k}: {val}")
    lines.append("")
    lines.append("## Notebook ACL 范围分布")
    lines.append("")
    ad = result.get("acl_distribution")
    if isinstance(ad, dict):
        for k in ("company", "admin", "group", "unknown", "missing_group", "total"):
            lines.append(f"- {k}: {ad.get(k)}")
    else:
        lines.append(f"- {ad}")
    lines.append("")
    lines.append("## 回填候选 / blocked")
    lines.append("")
    bf = result.get("backfill")
    if isinstance(bf, dict) and not bf.get("unavailable"):
        for k in ("candidate", "already_active", "blocked_disabled",
                  "blocked_unknown", "blocked_archived_default"):
            lines.append(f"- {k}: {bf.get(k)}")
        lines.append(f"- 规则说明：{bf.get('note')}")
    lines.append("")
    lines.append("## 异常候选（脱敏计数）")
    lines.append("")
    lines.append(f"- 同 ACL 不同 Notebook：{result.get('same_acl')}")
    lines.append(f"- 同名 Wiki 冲突候选：{result.get('wiki_name_conflicts')}")
    lines.append(f"- orphan：{result.get('orphans')}")
    lines.append("")
    if result.get("missing_tables"):
        lines.append(f"## 缺失表\n\n{result['missing_tables']}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return path
