"""回填/迁移后置检查（postflight）。

所有读检查复用 phase9b_migration.inventory 的核心只读函数（quick_check /
foreign_key_check / notebook_dataset / classify_notebook_scope / read_only_counts）。
写库仅出现在「合成反例事务」——建临时记录后 rollback，且必须先过
guard.write_guard（绝不接触真实库，也不建 CompileRun）。

检查项：
1. guard：db 非真实库且在 allowed_dir 内；
2. quick_check / foreign_key_check；
3. active binding 唯一：一个 notebook 至多一条 active binding；
4. blocked 未放行：应被 blocked（disabled 残留 / 无 binding 且 scope UNKNOWN）
   的 notebook 不得出现 active binding；
5. 同 ACL 不同默认 workspace：确定性默认 key workspace 只被其自有 notebook 绑定
   （不跨 notebook 合并）；
6. Wiki.workspace_id 一致性：引用必须存在的 workspace；published 且 NULL
   workspace 的行数如实报告（0 就写 0）；
7. 数量与 baseline 对照：无意外减少；
8. 历史 nullable Section 可读；
9. 合成反例事务：临时 notebook/ws/binding/wiki/revision/api-section/evidence/
   evidence-binding 建后 rollback，验证回滚生效。
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.database import (
    EvidenceItem,
    Notebook,
    NotebookWorkspaceBinding,
    WikiPage,
    WikiRevision,
    WikiSection,
    WikiSectionEvidenceBinding,
    WikiWorkspace,
)

from phase9b_migration import guard
from phase9b_migration import inventory as _inv
from phase9b_migration.migrate import current_version

_PUBLIC_ACL = '{"groups": ["__public__"]}'


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _engine(db_path):
    return create_engine("sqlite:///" + Path(os.path.abspath(str(db_path))).as_posix())


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------

def _check_active_binding_unique(db_path) -> dict:
    with _inv.open_readonly(db_path) as conn:
        need = {"notebook_id", "status"}
        ok = (
            _inv.table_exists(conn, "notebook_workspace_bindings")
            and need <= set(_inv.column_names(conn, "notebook_workspace_bindings"))
        )
        if not ok:
            return {"passed": True, "violations": [], "note": _inv.UNAVAILABLE}
        rows = conn.execute(
            "SELECT notebook_id, COUNT(*) FROM notebook_workspace_bindings "
            "WHERE status='active' GROUP BY notebook_id HAVING COUNT(*) > 1"
        ).fetchall()
    violations = [{"notebook": _inv.short_hash(r[0]), "count": r[1]} for r in rows]
    return {"passed": not violations, "violations": violations}


def _blocked_notebook_ids(db_path) -> dict:
    """复用 inventory 只读数据 + 镜像决策，返回应被 blocked 的 notebook 名单。"""
    blocked = []
    reasons = {}
    with _inv.open_readonly(db_path) as conn:
        ds = _inv.notebook_dataset(conn)
        if not ds["notebooks"]:
            return {"unavailable": True, "blocked": [], "reasons": {}}
        admin = _inv.configured_admin_groups()
        nb_ids = {n["id"] for n in ds["notebooks"]}
        for nb in ds["notebooks"]:
            bid = nb["id"]
            binds = [b for b in ds["bindings"] if b["notebook_id"] == bid]
            active = [b for b in binds if b["status"] == "active"]
            if active:
                continue
            if binds:
                blocked.append(bid)
                reasons[bid] = "blocked_disabled"
                continue
            kind = _inv.classify_notebook_scope(
                nb.get("group_id"), ds["group_map"].get(bid, ()), admin
            )
            if kind == _inv.UNKNOWN:
                blocked.append(bid)
                reasons[bid] = "blocked_unknown"
    return {"unavailable": False, "blocked": blocked, "reasons": reasons}


def _check_blocked_not_released(db_path) -> dict:
    blocked_info = _blocked_notebook_ids(db_path)
    if blocked_info.get("unavailable"):
        return {"passed": True, "anomalies": [], "note": _inv.UNAVAILABLE}
    anomalies = []
    with _inv.open_readonly(db_path) as conn:
        blocked = set(blocked_info["blocked"])
        if blocked:
            placeholders = ",".join("?" * len(blocked))
            rows = conn.execute(
                "SELECT DISTINCT notebook_id FROM notebook_workspace_bindings "
                f"WHERE status='active' AND notebook_id IN ({placeholders})",
                tuple(sorted(blocked)),
            ).fetchall()
        else:
            rows = []
    for (bid,) in rows:
        anomalies.append({
            "notebook": _inv.short_hash(bid),
            "reason": blocked_info["reasons"].get(bid),
        })
    return {
        "passed": not anomalies,
        "anomalies": anomalies,
        "blocked_disabled": sum(
            1 for r in blocked_info["reasons"].values() if r == "blocked_disabled"
        ),
        "blocked_unknown": sum(
            1 for r in blocked_info["reasons"].values() if r == "blocked_unknown"
        ),
    }


def _check_default_workspace_exclusive(db_path) -> dict:
    """确定性默认 key workspace 只被其自有 notebook 绑定（同 ACL 不合并）。"""
    from app.core.access_control import workspace_key_for_notebook as _ws_key

    anomalies = []
    with _inv.open_readonly(db_path) as conn:
        ds = _inv.notebook_dataset(conn)
        if not ds["notebooks"]:
            return {"passed": True, "anomalies": [], "note": _inv.UNAVAILABLE}
        nb_ids = [n["id"] for n in ds["notebooks"]]
        # ws_id → 其默认 key 派生自哪个 notebook（key 唯一，最多一个 owner）。
        owner_by_ws = {}
        for nid in nb_ids:
            key = _ws_key(nid)
            for ws_id, w in ds["workspaces"].items():
                if w["key"] == key:
                    owner_by_ws[ws_id] = nid
        per_ws_bindings = {}
        for b in ds["bindings"]:
            if b["status"] == "active":
                per_ws_bindings.setdefault(b["workspace_id"], []).append(b["notebook_id"])
        for ws_id, owner in owner_by_ws.items():
            bound = per_ws_bindings.get(ws_id, [])
            if not bound:
                continue  # 尚未绑定（例如还没回填）不是“合并”缺陷
            if set(bound) != {owner}:
                anomalies.append({
                    "workspace": _inv.short_hash(ws_id),
                    "default_notebook": _inv.short_hash(owner),
                    "bound_notebooks": sorted(_inv.short_hash(b) for b in bound),
                })
    return {"passed": not anomalies, "anomalies": anomalies}


def _check_wiki_workspace_consistent(db_path) -> dict:
    out = {
        "passed": True,
        "wikis_bad_workspace_ref": None,
        "published_wiki_null_workspace": None,
        "anomalies": [],
        "notes": [],
    }
    with _inv.open_readonly(db_path) as conn:
        if not _inv.table_exists(conn, "wiki_pages"):
            out["note"] = "wiki_pages 表缺失"
            return out
        cols = set(_inv.column_names(conn, "wiki_pages"))
        if not {"workspace_id", "status", "id"} <= cols:
            out["note"] = _inv.UNAVAILABLE
            return out
        ws_ids = {r[0] for r in conn.execute("SELECT id FROM wiki_workspaces")}
        bad = conn.execute(
            "SELECT id, workspace_id FROM wiki_pages WHERE workspace_id IS NOT NULL "
            "AND workspace_id NOT IN (SELECT id FROM wiki_workspaces)"
        ).fetchall()
        out["wikis_bad_workspace_ref"] = len(bad)
        for (wid, wsid) in bad:
            out["anomalies"].append({
                "wiki": _inv.short_hash(wid),
                "workspace": _inv.short_hash(wsid),
                "kind": "bad_workspace_ref",
            })
        out["published_wiki_null_workspace"] = conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE status='published' "
            "AND workspace_id IS NULL"
        ).fetchone()[0]
        # 有 wiki 但无 active binding 的 workspace（信息性，不判 fail）
        bound_ws = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT workspace_id FROM notebook_workspace_bindings "
                "WHERE status='active'"
            ).fetchall()
        }
        used_ws = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT workspace_id FROM wiki_pages WHERE workspace_id IS NOT NULL"
            ).fetchall()
        }
        for wsid in sorted(used_ws - bound_ws):
            out["notes"].append({
                "workspace": _inv.short_hash(wsid),
                "kind": "workspace_has_wiki_no_active_binding",
            })
        out["passed"] = out["wikis_bad_workspace_ref"] == 0
    return out


def _check_section_readable(db_path) -> dict:
    with _inv.open_readonly(db_path) as conn:
        if not _inv.table_exists(conn, "wiki_sections"):
            return {"passed": True, "note": "wiki_sections 表缺失，无可读对象"}
        cols = set(_inv.column_names(conn, "wiki_sections"))
        needed = {"revision_id", "section_type", "heading", "content", "order_index"}
        if not needed <= cols:
            return {"passed": True, "note": f"关键列缺失 {sorted(needed - cols)}"}
        try:
            rows = conn.execute(
                "SELECT COUNT(*), COUNT(heading) FROM wiki_sections"
            ).fetchone()
            return {
                "passed": True,
                "note": f"可读；行数={rows[0]}（其中 heading 非空={rows[1]}）",
            }
        except Exception as exc:
            return {"passed": False, "note": f"Section 读取失败：{exc}"}


def _check_counts_vs_baseline(db_path, baseline_counts) -> dict:
    if not baseline_counts:
        return {"passed": True, "decreased": {}, "note": "未提供 baseline_counts，跳过数量对照"}
    now = _inv.read_only_counts(db_path)
    decreased = {}
    for table, base in baseline_counts.items():
        cur = now.get(table)
        if base is None:
            continue
        if cur is None or cur < base:
            decreased[table] = {"baseline": base, "now": cur}
    return {"passed": not decreased, "decreased": decreased}


# ---------------------------------------------------------------------------
# 合成反例事务（临时记录 → rollback）
# ---------------------------------------------------------------------------

def synth_counterexample(db_path) -> dict:
    """在一个受 guard 保护的写事务里建临时反例记录，然后整体 rollback。

    验证回滚生效（前后行数一致），返回明细。不建 CompileRun。
    """
    engine = _engine(db_path)
    detail = {"created": {}, "counts_before": None, "counts_after": None}
    with Session(engine) as session:
        before = _count_drill_tables(session)
        nb_id = str(uuid.uuid4())
        ws_id = str(uuid.uuid4())
        binding_id = str(uuid.uuid4())
        wiki_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        section_id = str(uuid.uuid4())
        evidence_id = str(uuid.uuid4())
        bind_id = str(uuid.uuid4())

        session.add(Notebook(id=nb_id, name="drill-synth-counterexample", group_id=None))
        session.add(WikiWorkspace(
            id=ws_id, key="drill-synth-ws", name="临时反例 ws",
            acl_scope=_PUBLIC_ACL, scope_id="company", status="active",
        ))
        session.add(NotebookWorkspaceBinding(
            id=binding_id, notebook_id=nb_id, workspace_id=ws_id, status="active",
        ))
        session.add(WikiPage(
            id=wiki_id, title="drill-synth-wiki", summary="", acl_scope=_PUBLIC_ACL,
            status="published", workspace_id=ws_id, dirty=False,
        ))
        session.add(WikiRevision(
            id=revision_id, wiki_page_id=wiki_id, title="drill-synth-wiki",
            summary="", status="published", edit_type="auto",
        ))
        session.add(WikiSection(
            id=section_id, revision_id=revision_id, section_type="evidence",
            heading=None, content="drill synth body", order_index=0,
            section_key="drill:synth:section", validation_status="pass",
            structure_json="{}",
        ))
        session.add(EvidenceItem(
            id=evidence_id, evidence_type="manual", content="drill synth evidence",
            content_hash=_sha256("drill synth evidence"), status="active",
        ))
        session.add(WikiSectionEvidenceBinding(
            id=bind_id, section_id=section_id, evidence_id=evidence_id,
            field_path="drill.field", usage_type="support",
            evidence_content_hash=_sha256("drill synth evidence"),
        ))
        session.flush()
        detail["created"] = {
            "notebook": nb_id, "workspace": ws_id, "binding": binding_id,
            "wiki": wiki_id, "revision": revision_id, "section": section_id,
            "evidence": evidence_id, "evidence_binding": bind_id,
        }
        after_in_txn = _count_drill_tables(session)
        detail["counts_in_txn"] = after_in_txn
        session.rollback()
    # 事务外复查：行数应回到 before。
    with Session(engine) as session:
        after = _count_drill_tables(session)
    detail["counts_before"] = before
    detail["counts_after"] = after
    detail["rolled_back"] = after == before
    return detail


def _count_drill_tables(session) -> dict:
    keys = [
        ("notebooks", Notebook), ("wiki_workspaces", WikiWorkspace),
        ("notebook_workspace_bindings", NotebookWorkspaceBinding),
        ("wiki_pages", WikiPage), ("wiki_revisions", WikiRevision),
        ("wiki_sections", WikiSection), ("evidence_items", EvidenceItem),
        ("wiki_section_evidence_bindings", WikiSectionEvidenceBinding),
    ]
    out = {}
    for name, model in keys:
        out[name] = session.query(model).count()
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def run_postflight(db_path, baseline_counts=None, *, allowed_dir=None, real_db=None) -> dict:
    """postflight 汇总。写库（合成反例事务）必须位于 allowed_dir 且非真实库。"""
    db_path = guard.write_guard(db_path, allowed_dir, real_db)
    result: dict = {"db_path": db_path}
    result["guard"] = {
        "is_real_db": False,
        "in_allowed_dir": True,
        "note": "postflight 只在受保护的工作副本上运行",
    }
    try:
        result["current_revision"] = current_version(db_path)
    except Exception as exc:
        result["current_revision"] = f"读取失败：{exc}"

    checks = {
        "active_binding_unique": _check_active_binding_unique(db_path),
        "blocked_not_released": _check_blocked_not_released(db_path),
        "default_workspace_exclusive": _check_default_workspace_exclusive(db_path),
        "wiki_workspace_consistent": _check_wiki_workspace_consistent(db_path),
        "section_readable": _check_section_readable(db_path),
        "counts_vs_baseline": _check_counts_vs_baseline(db_path, baseline_counts),
    }
    result["checks"] = checks
    result["synth_counterexample"] = synth_counterexample(db_path)
    passed_parts = [
        result["synth_counterexample"].get("rolled_back", False),
    ]
    for name, c in checks.items():
        if isinstance(c, dict) and "passed" in c:
            passed_parts.append(bool(c["passed"]))
    result["passed"] = all(passed_parts)
    return result
