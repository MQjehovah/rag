"""钉钉资料权限重组 + Wiki 正式重建 + 历史 Community Wiki 清理（一次性安全脚本）。

分三阶段，每阶段独立 dry-run / --apply / --result-json：
- reorg：建 admin-only Notebook + 建顶级目录映射 + 重新归属 144 Page + 恢复
  SourceItem active + 同步 Evidence + 重建 V4 图谱 provenance（不调 LLM）。
- wiki：用真实 LLM 重建 Page 驱动 Wiki（复用 build_wiki_from_pages，两阶段去重）。
- cleanup：dry-run/apply 清理历史旧 Community「社区 1」（复用 legacy_wiki_cleanup）。

硬约束：
- 显式 --db（必填），禁止从 settings.database_url 隐式连接真实库；
- 默认 dry-run（零写入，wiki 阶段 dry-run 不调 LLM）；
- 显式 --apply 才写入 / 调用 LLM；
- 幂等、可安全续跑；路径与 ID 精确匹配；任一冲突 fail closed；不吞异常；
- 不重新实现 ACL / 图谱 / Wiki / 清理算法，全部复用现有服务。

用法：
    .venv/Scripts/python.exe scripts/reorganize_dingtalk_permissions.py \
        --db <绝对路径> --stage reorg [--apply] [--result-json out.json]
    .venv/Scripts/python.exe scripts/reorganize_dingtalk_permissions.py \
        --db <绝对路径> --stage wiki  [--apply] [--result-json out.json]
    .venv/Scripts/python.exe scripts/reorganize_dingtalk_permissions.py \
        --db <绝对路径> --stage cleanup [--apply] [--result-json out.json]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core import access_control  # noqa: E402
from app.core.path_mapping import (  # noqa: E402
    normalize_folder_path,
    resolve_target_notebook_id,
)
from app.models.database import (  # noqa: E402
    Notebook,
    Page,
    SourceConnection,
    SourceItem,
    SourcePathMapping,
    get_engine,
)

# 业务决策（本脚本唯一的权威配置，不读其他来源）。
SPACE_ID = "1oam4Sw4xLqdR78K"
ROOT_NOTEBOOK_ID = "c0385c0e-d7b9-4dcf-9e72-bea8cc2c4036"  # 原「钉钉知识库」（admin-only，根映射兜底）
CONNECTION_ID = "eb335f51-9757-412f-ace8-693cb4891aae"

# 新建 Notebook 定义（全部 admin-only，group_id=__local_admin__，notebook_groups 空）。
NEW_NOTEBOOKS = [
    {"name": "钉钉知识库（DMS 交付资料）"},
    {"name": "FAE 内部知识库"},
]

# 新增顶级目录映射：folder_path -> 目标 Notebook name（按定义顺序，先 DMS 后两个 FAE）。
TOP_LEVEL_MAPPINGS = [
    {"folder_path": "【DMS】交付服务部资料库", "notebook_name": "钉钉知识库（DMS 交付资料）"},
    {"folder_path": "【内部】FAE工作管理", "notebook_name": "FAE 内部知识库"},
    {"folder_path": "【内部】FAE知识库", "notebook_name": "FAE 内部知识库"},
]


class ReorgError(RuntimeError):
    """校验或写入失败（fail closed，不产生部分写入）。"""


def _resolve_abs_db(path: str) -> Path:
    if not path:
        raise ReorgError("--db 必填：必须显式指定数据库绝对路径")
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        raise ReorgError(f"--db 是目录，不是数据库文件：{p}")
    if not p.exists():
        raise ReorgError(f"--db 数据库文件不存在：{p}")
    return p


def _space_id_from_acl(item) -> str | None:
    raw = getattr(item, "acl_json", None) or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        sid = data.get("space_id")
        return str(sid) if sid else None
    return None


# ---------------------------------------------------------------------------
# Stage reorg
# ---------------------------------------------------------------------------

def _find_notebook_by_name(db, name: str):
    rows = db.query(Notebook).filter(Notebook.name == name).all()
    if len(rows) > 1:
        raise ReorgError(f"Notebook 名称重复（{name}），拒绝猜测")
    return rows[0] if rows else None


def _find_mapping(db, connection_id: str, space_id: str, folder_path: str):
    rows = (
        db.query(SourcePathMapping)
        .filter(
            SourcePathMapping.connection_id == connection_id,
            SourcePathMapping.path_namespace == space_id,
            SourcePathMapping.folder_path == folder_path,
        )
        .all()
    )
    if len(rows) > 1:
        raise ReorgError(f"映射重复（conn={connection_id} ns={space_id} folder={folder_path}），拒绝猜测")
    return rows[0] if rows else None


def _stage_reorg(db, *, apply: bool) -> dict:
    """建 Notebook + 建映射 + reassign + Evidence + 图谱重建。"""
    result = {
        "stage": "reorg",
        "notebooks": [],
        "mappings": [],
        "reassign": {},
        "evidence_synced": 0,
        "graph_rebuilt": 0,
        "error": None,
    }
    return _run_reorg(db, apply, result)


def _run_reorg(db, apply, result):
    try:
        _do_reorg(db, apply, result)
    except Exception as exc:
        db.rollback()
        result["error"] = str(exc)
    return result


def _do_reorg(db, apply, result):
    """reorg 实际逻辑（异常由 _run_reorg 捕获）。"""

    # ---- 1. 创建/复用 admin-only Notebook ----
    # 无论 dry-run 还是 apply，都真实创建并 flush 进事务（dry-run 最后统一 rollback），
    # 使 resolve_target_notebook_id 能正确匹配，从而精确预测 apply 后的 Page 分布。
    notebook_ids: dict[str, str] = {}  # name -> id
    for spec in NEW_NOTEBOOKS:
        name = spec["name"]
        existing = _find_notebook_by_name(db, name)
        if existing is None:
            nb = Notebook(id=str(uuid.uuid4()), name=name, group_id="__local_admin__")
            db.add(nb)
            db.flush()
            notebook_ids[name] = nb.id
            result["notebooks"].append({"name": name, "id": nb.id, "action": "created"})
        else:
            # 已存在：必须是 admin-only（group_id=__local_admin__ 且无额外业务组）
            kind = access_control.scope_from_notebook(db, existing).kind
            if kind != access_control.SCOPE_ADMIN:
                raise ReorgError(f"Notebook「{name}」已存在但非 admin scope（{kind}），fail closed")
            if existing.group_id != "__local_admin__":
                raise ReorgError(f"Notebook「{name}」group_id 非 __local_admin__，fail closed")
            notebook_ids[name] = existing.id
            result["notebooks"].append({"name": name, "id": existing.id, "action": "existing"})

    # ---- 2. 创建/校验三个顶级目录映射（根映射不动） ----
    for spec in TOP_LEVEL_MAPPINGS:
        folder_path = normalize_folder_path(spec["folder_path"])
        nb_name = spec["notebook_name"]
        nb_id = notebook_ids.get(nb_name)
        if not nb_id:
            raise ReorgError(f"目标 Notebook「{nb_name}」未解析，fail closed")
        existing = _find_mapping(db, CONNECTION_ID, SPACE_ID, folder_path)
        if existing is None:
            db.add(SourcePathMapping(
                id=str(uuid.uuid4()),
                connection_id=CONNECTION_ID,
                path_namespace=SPACE_ID,
                folder_path=folder_path,
                notebook_id=nb_id,
                created_by="reorganize",
            ))
            db.flush()
            result["mappings"].append({"folder_path": folder_path, "notebook_name": nb_name, "action": "created"})
        else:
            if existing.notebook_id != nb_id:
                raise ReorgError(
                    f"映射「{folder_path}」已指向其他 Notebook（{existing.notebook_id}），fail closed"
                )
            result["mappings"].append({"folder_path": folder_path, "notebook_name": nb_name, "action": "existing"})

    # 校验根映射仍指向原 admin Notebook（不删除不修改）
    root_mapping = _find_mapping(db, CONNECTION_ID, SPACE_ID, "")
    if root_mapping is None:
        raise ReorgError("根目录映射缺失，fail closed（应保留兜底）")
    if root_mapping.notebook_id != ROOT_NOTEBOOK_ID:
        raise ReorgError(f"根目录映射指向异常（{root_mapping.notebook_id}），fail closed")

    # ---- 3. reassign：遍历 144 个 active dingtalk SourceItem ----
    items = (
        db.query(SourceItem)
        .join(SourceConnection, SourceConnection.id == SourceItem.connection_id)
        .filter(
            SourceConnection.connector_key == "dingtalk",
            SourceItem.state == "active",
            SourceItem.page_id.isnot(None),
        )
        .order_by(SourceItem.id)
        .all()
    )

    assign_plan: dict[str, int] = {}  # notebook_id -> page count
    reassigned = 0
    already_ok = 0
    for item in items:
        page = db.get(Page, item.page_id)
        if page is None:
            raise ReorgError(f"active SourceItem 关联的 Page 不存在：{item.page_id}")
        space = _space_id_from_acl(item)
        if space != SPACE_ID:
            raise ReorgError(f"SourceItem space 异常（{space!r}），fail closed")
        target = resolve_target_notebook_id(db, CONNECTION_ID, space, item.source_path)
        if target is None:
            # 未映射：应回落到根映射（原 admin Notebook）
            target = ROOT_NOTEBOOK_ID
        assign_plan[target] = assign_plan.get(target, 0) + 1
        if page.notebook_id != target:
            if apply:
                page.notebook_id = target
            reassigned += 1
        else:
            already_ok += 1

    result["reassign"] = {
        "total_source_items": len(items),
        "reassigned": reassigned,
        "already_ok": already_ok,
        "by_notebook": {str(k): v for k, v in assign_plan.items()},
    }

    if apply:
        db.commit()

        # ---- 4. 同步 Evidence（幂等） ----
        for item in items:
            from app.core.evidence_ingest import sync_page_evidence
            sync_page_evidence(db, item.page_id)
        db.commit()
        result["evidence_synced"] = len(items)

        # ---- 5. 重建 V4 图谱 provenance（幂等，同步可等待） ----
        from app.core.knowledge_compiler_v3.v4_graph_builder import rebuild_page_graph
        for item in items:
            rebuild_page_graph(db, item.page_id, commit=True)
        result["graph_rebuilt"] = len(items)
    else:
        # dry-run：不写库、不 sync evidence、不重建图谱
        db.rollback()

    return result


# ---------------------------------------------------------------------------
# Stage wiki
# ---------------------------------------------------------------------------

def _stage_wiki(db, *, apply: bool) -> dict:
    """用真实 LLM 重建 144 个有效 Page 的 Wiki（dry-run 只报告，不调 LLM）。"""
    result = {
        "stage": "wiki",
        "eligible_pages": 0,
        "llm_configured": False,
        "model": None,
        "stats": None,
        "wiki_titles": [],
        "error": None,
    }
    from app.config import settings
    result["llm_configured"] = bool(settings.llm_api_url and settings.llm_model)
    result["model"] = settings.llm_model

    pages = (
        db.query(Page)
        .filter(Page.notebook_id.isnot(None))
        .order_by(Page.id)
        .all()
    )
    result["eligible_pages"] = len(pages)

    if not apply:
        return result  # dry-run：不调 LLM

    if not result["llm_configured"]:
        raise ReorgError("LLM 未配置，无法重建 Wiki")

    import asyncio
    from app.core.knowledge_compiler_v3.wiki_page_builder import (
        call_wiki_llm_json,
        _legacy_build_wiki_from_pages,
    )

    stats = asyncio.run(
        _legacy_build_wiki_from_pages(
            db, pages, llm_json=call_wiki_llm_json, dedupe_synthesis=True)
    )
    result["stats"] = stats

    # 收集重建后的有效 Page 驱动 Wiki 标题（status=published，非社区 N）
    from app.models.database import WikiPage
    rows = db.query(WikiPage).filter(WikiPage.status == "published").order_by(WikiPage.title).all()
    result["wiki_titles"] = [w.title for w in rows]
    result["wiki_count"] = len(rows)
    return result


# ---------------------------------------------------------------------------
# Stage cleanup
# ---------------------------------------------------------------------------

def _stage_cleanup(db, *, apply: bool) -> dict:
    """清理历史旧 Community「社区 1」（复用 legacy_wiki_cleanup）。"""
    from app.core.knowledge_compiler_v3.legacy_wiki_cleanup import cleanup_legacy_wikis
    report = cleanup_legacy_wikis(db, apply=apply)
    return {
        "stage": "cleanup",
        "total_legacy": report.total_legacy,
        "cleanable_count": len(report.cleanable),
        "human_protected_count": len(report.human_protected),
        "cleanable": [
            {"wiki_page_id": v.wiki_page_id, "title": v.title, "status": v.status}
            for v in report.cleanable
        ],
        "human_protected": [
            {"wiki_page_id": v.wiki_page_id, "title": v.title, "status": v.status, "reasons": v.reasons}
            for v in report.human_protected
        ],
        "applied": report.applied,
        "error": None,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="钉钉权限重组 / Wiki 重建 / 历史 Wiki 清理（默认 dry-run）")
    parser.add_argument("--db", required=True, help="SQLite 数据库文件绝对路径")
    parser.add_argument("--stage", required=True, choices=["reorg", "wiki", "cleanup"])
    parser.add_argument("--apply", action="store_true", help="实际写入 / 调用 LLM（默认 dry-run）")
    parser.add_argument("--result-json", default=None, help="写入机器可读结果 JSON")
    args = parser.parse_args(argv)

    abs_db = _resolve_abs_db(args.db)
    print(f"[info] 数据库绝对路径（解析后）：{abs_db}")

    engine = get_engine(f"sqlite:///{abs_db.as_posix()}")
    db = sessionmaker(bind=engine)()

    try:
        if args.stage == "reorg":
            result = _stage_reorg(db, apply=args.apply)
        elif args.stage == "wiki":
            result = _stage_wiki(db, apply=args.apply)
        elif args.stage == "cleanup":
            result = _stage_cleanup(db, apply=args.apply)
        else:
            raise ReorgError(f"未知 stage：{args.stage}")
    except Exception as exc:
        db.rollback()
        result = {"stage": args.stage, "apply": args.apply, "error": str(exc)}
    finally:
        db.close()
        engine.dispose()

    result["apply"] = args.apply
    result["database"] = str(abs_db)

    if args.result_json:
        Path(args.result_json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("error") is not None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
