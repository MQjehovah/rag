"""Workspace 回填（backfill）—— 复用 production 确定性路由语义。

规则（决策对象 = notebook；对齐 backend/app/core/wiki_workspace/routing.py
ensure_notebook_workspace，本模块逐条镜像并引用 file:line）：
1. 已有 active binding → skip_active（routing.py:103-109，显式共享优先）；
2. 无 active 但存在任一 binding（disabled 残留）→ blocked_disabled
   （routing.py:110-112，管理员意图保持未绑定，不自动重建）；
3. 完全无 binding 且 scope UNKNOWN/无法解析 → blocked_unknown
   （routing.py:113-115，fail closed）；
4. 完全无 binding、scope 可解析但该 notebook 确定性默认 key 的 workspace 已
   archived → blocked_archived（routing.py:117-121，归档区不再接收新绑定）；
5. 其余 → 建确定性默认 workspace（key=workspace_key_for_notebook(notebook.id)，
   acl=acl_json_for_scope(scope)，name=scope_id，scope_id 规范化）+ active binding
   （routing.py:122-141）。同 key 已 active 则复用该默认 workspace。

复用点（production 语义的单一事实来源）：
- access_control.scope_from_notebook / normalize_scope_id /
  workspace_key_for_notebook / acl_json_for_scope / SCOPE_UNKNOWN
  （backend/app/core/access_control.py:186-217, 330-342, 345-353, 364-370）；
- 这些是纯函数，通过 SQLAlchemy Session 绑定本工具的自建 engine 使用；
- 不合并同 ACL：每个 notebook 各建各的默认 workspace（routing.py:14-15 注释）。

apply 语义：plan 带确定性 hash（内容 = counts + 有序 items）。dry-run 后若库被
外部改动（plan_now hash ≠ 给定 hash）→ 拒绝执行。执行在单事务内完成，失败整体
回滚。二次 apply：plan_now.created==0 → 直接返回 0（幂等）。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import access_control
from app.core.access_control import (
    SCOPE_UNKNOWN,
    acl_json_for_scope,
    normalize_scope_id,
    scope_from_notebook,
    workspace_key_for_notebook,
)
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    WikiWorkspace,
)

from phase9b_migration import guard

# 决策值
DECISION_ACTIVE = "active"                    # 已存在 active binding（skip）
DECISION_BLOCKED_DISABLED = "blocked_disabled"
DECISION_BLOCKED_UNKNOWN = "blocked_unknown"
DECISION_BLOCKED_ARCHIVED = "blocked_archived"
DECISION_CREATE = "create"


class BackfillError(RuntimeError):
    """回填失败基类。"""


class PlanChangedError(BackfillError):
    """dry-run 后数据库被外部改动，计划 hash 不一致。"""


def _engine(db_path):
    url = "sqlite:///" + Path(os.path.abspath(str(db_path))).as_posix()
    return create_engine(url)


def _classify(session: Session, notebook: Notebook):
    """返回 (decision, scope|None)。decision 对应模块头注释的五条规则。"""
    active = (
        session.query(NotebookWorkspaceBinding)
        .filter(
            NotebookWorkspaceBinding.notebook_id == notebook.id,
            NotebookWorkspaceBinding.status == "active",
        )
        .first()
    )
    if active is not None:
        return DECISION_ACTIVE, None
    any_binding = (
        session.query(NotebookWorkspaceBinding)
        .filter(NotebookWorkspaceBinding.notebook_id == notebook.id)
        .first()
    )
    if any_binding is not None:
        # routing.py:110-112：disabled 残留 = 管理员意图不路由 → 不自动重建。
        return DECISION_BLOCKED_DISABLED, None
    scope = scope_from_notebook(session, notebook)
    if scope.kind == SCOPE_UNKNOWN:
        return DECISION_BLOCKED_UNKNOWN, scope
    scope_id = normalize_scope_id(scope)
    key = workspace_key_for_notebook(notebook.id)
    existing = (
        session.query(WikiWorkspace).filter(WikiWorkspace.key == key).first()
    )
    if existing is not None and existing.status != "active":
        # routing.py:117-121：默认私用空间已归档 → 不再自动接收新绑定。
        return DECISION_BLOCKED_ARCHIVED, scope
    return DECISION_CREATE, scope


def _item(notebook: Notebook, scope, decision: str) -> dict:
    item = {
        "notebook_id": notebook.id,
        "decision": decision,
        "notebook_key": short_hash(notebook.id),
    }
    if decision != DECISION_CREATE:
        return item
    scope_id = normalize_scope_id(scope)
    item.update({
        "scope_kind": scope.kind,
        "scope_id": scope_id,
        "acl_scope": acl_json_for_scope(scope),
        "workspace_key": workspace_key_for_notebook(notebook.id),
        "workspace_name": scope_id,
    })
    return item


def short_hash(identifier) -> str:
    return hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:12]


def plan(db_path) -> dict:
    """只读计算回填计划（含确定性 plan_hash）。只 SELECT，不写库。"""
    db_path = os.path.abspath(str(db_path))
    engine = _engine(db_path)
    items: list[dict] = []
    with Session(engine) as session:
        for notebook in session.query(Notebook).order_by(Notebook.id).all():
            decision, scope = _classify(session, notebook)
            items.append(_item(notebook, scope, decision))
    counts = {d: sum(1 for it in items if it["decision"] == d) for d in (
        DECISION_CREATE, DECISION_ACTIVE, DECISION_BLOCKED_DISABLED,
        DECISION_BLOCKED_UNKNOWN, DECISION_BLOCKED_ARCHIVED,
    )}
    counts["total"] = len(items)
    payload = {"counts": counts, "items": items}
    plan_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "db_path": db_path,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_hash": plan_hash,
        "counts": counts,
        "items": items,
    }


def dry_run(db_path, out=None) -> dict:
    """dry-run：输出计划摘要 + plan_hash。只读，不改库。

    out：None → stdout；否则为可写文本路径（报告中 notebook id 一律短哈希）。
    """
    p = plan(db_path)
    lines = [
        "Phase 9B workspace 回填 dry-run（只读，未写库）",
        f"db: {p['db_path']}",
        f"plan_hash: {p['plan_hash']}",
        "counts: " + json.dumps(p["counts"], ensure_ascii=False),
    ]
    for it in p["items"]:
        lines.append(f"  {it['decision']:<16} notebook={it['notebook_key']}")
    text = "\n".join(lines) + "\n"
    if out:
        with open(os.path.abspath(str(out)), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        p["report_path"] = os.path.abspath(str(out))
    else:
        print(text, flush=True)
    return p


def apply_item(session: Session, item: dict) -> int:
    """单条 create 计划执行（被 monkeypatch 以注入失败，测试整体回滚）。"""
    key = item["workspace_key"]
    workspace = (
        session.query(WikiWorkspace).filter(WikiWorkspace.key == key).first()
    )
    if workspace is None:
        workspace = WikiWorkspace(
            id=str(uuid.uuid4()),
            key=key,
            name=item.get("workspace_name") or item["scope_id"],
            description=None,
            acl_scope=item["acl_scope"],
            scope_id=item["scope_id"],
            status="active",
            created_by="phase9b_migration.backfill",
        )
        session.add(workspace)
        session.flush()
    elif workspace.status != "active":
        raise BackfillError(
            f"默认 workspace 已归档，拒绝绑定：key={key} notebook={item['notebook_key']}"
        )
    session.add(
        NotebookWorkspaceBinding(
            id=str(uuid.uuid4()),
            notebook_id=item["notebook_id"],
            workspace_id=workspace.id,
            status="active",
            created_by="phase9b_migration.backfill",
        )
    )
    session.flush()
    return 1


def apply(db_path, plan_hash=None, *, allowed_dir=None, real_db=None) -> int:
    """执行回填计划（单事务；失败整体回滚）。

    - 写库 → 必须提供 allowed_dir（guard.write_guard 兜底）；
    - plan_now.created == 0 → 直接返回 0（幂等，即使给了旧 hash）；
    - 否则要求传入 plan_hash 与 plan_now 一致，否则抛 PlanChangedError 不改库；
    - 事务内逐条 apply_item；任一条失败 → rollback + 抛 BackfillError。
    """
    guard.write_guard(db_path, allowed_dir, real_db)
    db_path = os.path.abspath(str(db_path))
    plan_now = plan(db_path)
    if plan_now["counts"][DECISION_CREATE] == 0:
        return 0
    if plan_hash and plan_now["plan_hash"] != plan_hash:
        raise PlanChangedError(
            f"计划 hash 不一致（外部改动）：dry-run={plan_hash} now={plan_now['plan_hash']}"
        )
    engine = _engine(db_path)
    created = 0
    try:
        with Session(engine) as session, session.begin():
            for item in plan_now["items"]:
                if item["decision"] == DECISION_CREATE:
                    created += apply_item(session, item)
    except PlanChangedError:
        raise
    except Exception as exc:
        raise BackfillError(f"回填失败，已整体回滚：{exc}") from exc
    return created
