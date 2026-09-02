"""Phase 3.1：WikiWorkspace inventory（dry-run，只读，不写数据库）。

候选 workspace 生成规则（Phase 3.1：候选合并不再仅由 ACL scope 决定）：
- 每个 Notebook 一个默认候选：key = workspace_key_for_notebook(notebook.id)
  （同 ACL 多 notebook → 输出多个候选，每个 notebook 一个默认）；
- 有 active binding 的 Notebook → 归入该绑定 workspace 的候选（显式共享连通分量，
  key = workspace.key，多个 notebook 可绑定到同一 workspace 实现合并）；
- 有 disabled binding 且无 active binding → blocked（管理员已解绑/不路由，需人工决定）；
- Notebook scope UNKNOWN → blocked（fail closed，无法给出可用默认候选）；
- 报告保留 scope_id / acl_scope（权限/审计），并提示可用显式绑定合并同 ACL 候选。

Wiki 归属：
- Wiki 来源 page 全部落在同一候选 → 计入该候选 wiki_ids；
- 来源 page 跨多个候选（不同 workspace/默认）或含 blocked/无 notebook → blocked
  （跨 workspace 来源的 Wiki 无法在自动路由下成立，需人工拆/绑定）；
- 无来源 / 来源 page 不存在 → orphan（与旧语义一致）。

用法：
    cd backend
    & ..\\.venv\\Scripts\\python.exe scripts/inventory_wiki_workspaces.py --db-url "sqlite:///./data/notes.db"
    & ..\\.venv\\Scripts\\python.exe scripts/inventory_wiki_workspaces.py --db-url "sqlite:///./data/notes.db" --report ../docs/workspace-inventory.md

可被测试调用（run_inventory(db)）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from app.core import access_control
from app.models.database import (
    Notebook,
    NotebookWorkspaceBinding,
    Page,
    WikiPage,
    WikiWorkspace,
)


def _scope_of_notebook(db: Session, notebook: Notebook) -> tuple[str | None, str | None]:
    """返回 (scope_id, acl_scope)；无法确定 → (None, None)（unknown/fail closed）。"""
    if notebook is None:
        return None, None
    scope = access_control.scope_from_notebook(db, notebook)
    if scope.kind == access_control.SCOPE_UNKNOWN:
        return None, None
    return (
        access_control.normalize_scope_id(scope),
        access_control.acl_json_for_scope(scope),
    )


def run_inventory(db: Session) -> dict:
    """只读盘点：返回 {stats, workspaces, orphans, blocked}。绝不写数据库。"""
    notebooks = {n.id: n for n in db.query(Notebook).all()}
    pages = {p.id: p for p in db.query(Page).all()}
    wikis = {w.id: w for w in db.query(WikiPage).all()}
    existing_bindings = db.query(NotebookWorkspaceBinding).count()
    existing_workspaces = {ws.id: ws for ws in db.query(WikiWorkspace).all()}

    # notebook → 候选分组标识（"bind:<ws_id>" 或 "default:<nb_id>" 或 None=blocked）
    def _candidate_of(nb_id: str) -> str | None:
        bindings = (
            db.query(NotebookWorkspaceBinding)
            .filter(NotebookWorkspaceBinding.notebook_id == nb_id)
            .all()
        )
        active = [b for b in bindings if b.status == "active"]
        if active:
            return f"bind:{active[0].workspace_id}"
        if bindings:
            return None  # disabled binding（管理员不路由意图）→ blocked
        return f"default:{nb_id}"

    candidate_of: dict[str, str | None] = {
        nid: _candidate_of(nid) for nid in notebooks
    }
    scope_cache: dict[str, tuple[str | None, str | None]] = {
        nid: _scope_of_notebook(db, nb) for nid, nb in notebooks.items()
    }

    workspaces: list[dict] = []
    blocked: list[dict] = []
    blocked_nb_reason: dict[str, str] = {}

    # 1) Notebook 候选：每个 active binding workspace 一个候选 + 每个默认 notebook 一个候选。
    # cand → (key, existing_ws_id_or_None)
    cand_key: dict[str, tuple[str, str | None]] = {}
    cand_scope: dict[str, tuple[str, str]] = {}
    cand_notebooks: dict[str, list[str]] = defaultdict(list)
    for nb_id in notebooks:
        cand = candidate_of[nb_id]
        if cand is None:
            blocked_nb_reason[nb_id] = "disabled_binding_kept_unbound"
            continue
        if cand.startswith("bind:"):
            ws_id = cand[len("bind:"):]
            ws = existing_workspaces.get(ws_id)
            if ws is None or ws.status != "active":
                blocked_nb_reason[nb_id] = "bound_workspace_missing_or_archived"
                continue
            cand_key[cand] = (ws.key, ws.id)
            cand_scope[cand] = (ws.scope_id, ws.acl_scope)
        else:
            nb_id = cand[len("default:"):]
            scope_id, acl_scope = scope_cache[nb_id]
            if scope_id is None:
                blocked_nb_reason[nb_id] = "unknown_scope"
                continue
            key = access_control.workspace_key_for_notebook(nb_id)
            existing = None
            for ws in existing_workspaces.values():
                if ws.key == key:
                    existing = ws.id
                    break
            cand_key[cand] = (key, existing)
            cand_scope[cand] = (scope_id, acl_scope)
        cand_notebooks[cand].append(nb_id)

    for cand in sorted(cand_notebooks):
        nb_ids = cand_notebooks[cand]
        key, existing = cand_key[cand]
        scope_id, acl_scope = cand_scope[cand]
        workspaces.append({
            "key": key,
            "scope_id": scope_id,
            "acl_scope": acl_scope,
            "notebook_ids": sorted(nb_ids),
            "wiki_ids": [],
            "page_count": 0,
            "existing": existing,
            "merged_by": "explicit_binding" if cand.startswith("bind:") else "notebook_default",
        })

    # 2) 每个 Wiki：来源 page → notebook → 候选归属。跨候选 → blocked。
    cand_to_ws: dict[str, dict] = {}
    for cand in cand_notebooks:
        for ws in workspaces:
            if cand.startswith("bind:") and ws["existing"] == cand[len("bind:"):]:
                cand_to_ws[cand] = ws
                break
            if cand.startswith("default:") and ws["notebook_ids"] == [cand[len("default:"):]]:
                cand_to_ws[cand] = ws
                break

    for wid, w in wikis.items():
        try:
            raw = json.loads(w.source_page_ids or "[]")
        except (TypeError, ValueError):
            raw = []
        src_ids = [i for i in raw if isinstance(i, str) and i in pages]
        src_notebooks: set[str] = set()
        missing_nb = False
        for pid in src_ids:
            nb_id = pages[pid].notebook_id
            if nb_id and nb_id in notebooks:
                src_notebooks.add(nb_id)
            elif nb_id:
                missing_nb = True  # 指向不存在 notebook 的来源
            else:
                missing_nb = True
        if not src_ids:
            blocked.append({
                "wiki_id": wid,
                "title": w.title,
                "reason": "no_source_or_no_notebook",
                "notebook_ids": [],
                "wiki_ids": [wid],
                "scopes": [],
            })
            continue
        cands = {candidate_of[nb] for nb in src_notebooks}
        cands.discard(None)
        valid_cands = [c for c in cands if c in cand_to_ws]
        if len(valid_cands) == 1 and len(cands) == 1 and not missing_nb:
            cand = valid_cands[0]
            target = cand_to_ws[cand]
            target["wiki_ids"] = sorted(set(target["wiki_ids"]) | {wid})
        else:
            scopes = sorted({scope_cache[nb][0] or "unknown" for nb in src_notebooks})
            reasons: set[str] = set()
            for nb in src_notebooks:
                if candidate_of[nb] is None:
                    reasons.add("disabled_binding_source")
                elif scope_cache[nb][0] is None:
                    reasons.add("unknown_scope_source")
            if missing_nb:
                reasons.add("missing_source_notebook")
            if len({scope_cache[nb][0] for nb in src_notebooks if scope_cache[nb][0]}) > 1:
                reasons.add("cross_scope_sources")
            elif len(valid_cands) > 1:
                reasons.add("cross_workspace_sources")
            blocked.append({
                "wiki_id": wid,
                "title": w.title,
                "reason": ",".join(sorted(reasons)) if reasons else "cross_workspace_sources",
                "notebook_ids": sorted(src_notebooks),
                "wiki_ids": [wid],
                "scopes": scopes,
            })

    # 3) blocked notebook（unknown scope / disabled binding / 悬空绑定）。
    for nb_id, reason in blocked_nb_reason.items():
        if any(nb_id in ws["notebook_ids"] for ws in workspaces):
            continue
        blocked.append({
            "notebook_ids": [nb_id],
            "wiki_ids": [],
            "reason": reason,
            "scopes": [],
        })

    orphans = [b for b in blocked if b["reason"].startswith("no_source_or_no_notebook")]
    blocked = [b for b in blocked if not b["reason"].startswith("no_source_or_no_notebook")]
    for o in orphans:
        o.pop("wiki_ids", None)
        o.pop("scopes", None)

    # 4) page_count 统计。
    for ws in workspaces:
        nbs = set(ws["notebook_ids"])
        ws["page_count"] = sum(
            1 for p in pages.values() if p.notebook_id in nbs
        )

    # 5) 提示：同 ACL 存在多个 notebook 默认候选 → 可显式绑定合并。
    scope_to_defaults: dict[str, list[dict]] = defaultdict(list)
    for ws in workspaces:
        if ws["merged_by"] == "notebook_default":
            scope_to_defaults[ws["scope_id"]].append(ws)
    for scope_id, defaults in scope_to_defaults.items():
        if len(defaults) > 1:
            for ws in defaults:
                ws["merge_hint"] = f"同 ACL {scope_id} 多 notebook：可用 admin 显式绑定合并为共享 workspace"

    return {
        "stats": {
            "notebooks": len(notebooks),
            "pages": len(pages),
            "wikis": len(wikis),
            "existing_bindings": existing_bindings,
            "existing_workspaces": len(existing_workspaces),
            "workspace_candidates": len(workspaces),
            "orphans": len(orphans),
            "blocked": len(blocked),
        },
        "workspaces": workspaces,
        "orphans": orphans,
        "blocked": blocked,
    }


def _to_markdown(data: dict) -> str:
    lines = [
        "# WikiWorkspace 盘点报告（dry-run）",
        "",
        f"- 生成时间：{datetime.now().isoformat()}",
        f"- Notebook：{data['stats']['notebooks']}",
        f"- Page：{data['stats']['pages']}",
        f"- Wiki：{data['stats']['wikis']}",
        f"- 已有绑定：{data['stats']['existing_bindings']}",
        f"- 已有 workspace：{data['stats']['existing_workspaces']}",
        f"- workspace 候选：{data['stats']['workspace_candidates']}",
        f"- orphan：{data['stats']['orphans']}",
        f"- blocked：{data['stats']['blocked']}",
        "",
        "## workspace 候选",
        "",
        "| key | scope_id | merged_by | notebooks | wikis | 已存在 | 提示 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for ws in data["workspaces"]:
        lines.append(
            f"| {ws['key']} | {ws['scope_id']} | {ws.get('merged_by')} "
            f"| {len(ws['notebook_ids'])} | {len(ws['wiki_ids'])} "
            f"| {ws.get('existing') or '-'} | {ws.get('merge_hint') or ''} |"
        )
    lines.append("")
    lines.append("## orphan")
    lines.append("")
    if data["orphans"]:
        for o in data["orphans"]:
            lines.append(f"- `{o['wiki_id']}` {o['title']}（{o['reason']}）")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## blocked")
    lines.append("")
    if data["blocked"]:
        for b in data["blocked"]:
            lines.append(
                f"- notebooks={b.get('notebook_ids', [])} wikis={b.get('wiki_ids', [])}：{b['reason']}"
            )
    else:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WikiWorkspace inventory（dry-run，只读）")
    parser.add_argument("--db-url", default="", help="数据库 URL；缺省用 settings.database_url")
    parser.add_argument("--report", default="", help="Markdown 报告输出路径；缺省只输出 JSON 到 stdout")
    args = parser.parse_args(argv)

    if args.db_url:
        from app.models.database import get_engine
        engine = get_engine(args.db_url)
    else:
        from app.api.deps import get_shared_engine
        engine = get_shared_engine()
    db = Session(bind=engine)
    try:
        data = run_inventory(db)
    finally:
        db.close()
        engine.dispose()

    print(json.dumps(data, ensure_ascii=False, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(_to_markdown(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
