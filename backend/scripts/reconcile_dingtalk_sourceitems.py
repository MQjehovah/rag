"""钉钉历史 SourceItem 一次性 reconciliation（Phase J 真实生产落地专用）。

背景：
- 真实库 144 个 Page 已导入且全部 source_type='dingtalk'、notebook_id=admin，
  但 source_items 表为空，导致 backfill_v4_graph.py 的远程 Page 有效性判定
  把全部 144 页判为失效，图谱回填 buildable=0。
- 旧脚本 v3_backfill_dingtalk_sourceitems.py 存在以下问题，不得直接执行：
    1. 从 settings.database_url 隐式决定数据库，无 --db 参数，无法明确指向
       演练副本或真实库；
    2. 默认即写入（无 dry-run），无 --apply 门禁；
    3. 按 name 模糊查找 SourceConnection，可能误匹配或漏匹配；
    4. 不校验 Notebook 仍为 admin scope，存在扩大权限风险；
    5. 不校验根目录映射，未与 J-1 文件夹映射联动；
    6. 不校验 Page.source_type / Page.notebook_id 与映射结果一致；
    7. 无事务原子性，中途失败会留下半完成状态；
    8. 无机器可读 JSON 输出。

本脚本职责（只回填历史 SourceItem + 可选创建根目录映射，不触碰其他任何数据）：
- 显式 --db（必填，禁止从 settings.database_url 隐式决定）；
- 默认 dry-run（零写入），显式 --apply 才写入；
- --space-id / --connection-id / --notebook-id 必填；
- 根目录映射仅当显式 --create-root-mapping 才允许创建；
- 单事务：根映射 + 全部 SourceItem 原子写入，任一失败整体 rollback；
- 不创建 Connection / Notebook / Page；不修改 Page / Notebook / 权限；
- 不触发下载、转换、索引、Evidence、Wiki 或图谱；不调用外部服务。

用法：
    .venv/Scripts/python.exe scripts/reconcile_dingtalk_sourceitems.py \
        --db <数据库绝对路径> \
        --space-id 1oam4Sw4xLqdR78K \
        --connection-id eb335f51-9757-412f-ace8-693cb4891aae \
        --notebook-id c0385c0e-d7b9-4dcf-9e72-bea8cc2c4036 \
        --create-root-mapping            # dry-run
    （追加 --apply 才写入）
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core import access_control  # noqa: E402
from app.core.dingtalk_storage import DingTalkLocalStorage  # noqa: E402
from app.core.folder_mapping import (  # noqa: E402
    resolve_target_notebook_id_for_entry,
)
from app.models.database import (  # noqa: E402
    Notebook,
    Page,
    SourceConnection,
    SourceItem,
    SourcePathMapping,
    get_engine,
)


class ReconciliationError(RuntimeError):
    """校验或写入失败（fail closed，不产生部分写入）。"""


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _resolve_abs_db(path: str) -> Path:
    """解析并打印数据库绝对路径（禁止从 settings.database_url 隐式决定）。"""
    if not path:
        raise ReconciliationError("--db 必填：必须显式指定数据库绝对路径")
    p = Path(path).expanduser().resolve()
    if p.is_dir():
        raise ReconciliationError(f"--db 是目录，不是数据库文件：{p}")
    if not p.exists():
        raise ReconciliationError(f"--db 数据库文件不存在：{p}")
    return p


def _validate_source_connection(db, connection_id: str) -> SourceConnection:
    """SourceConnection 必须真实存在且 connector_key == 'dingtalk'；不按名称模糊查找。"""
    if not connection_id:
        raise ReconciliationError("--connection-id 必填")
    conn = db.get(SourceConnection, connection_id)
    if conn is None:
        raise ReconciliationError(f"SourceConnection 不存在：{connection_id}")
    if conn.connector_key != "dingtalk":
        raise ReconciliationError(
            f"SourceConnection.connector_key 非 dingtalk：{conn.connector_key}"
        )
    return conn


def _validate_notebook(db, notebook_id: str) -> Notebook:
    """Notebook 必须存在且仍为 admin scope；不修改 group_id / notebook_groups。"""
    if not notebook_id:
        raise ReconciliationError("--notebook-id 必填")
    nb = db.get(Notebook, notebook_id)
    if nb is None:
        raise ReconciliationError(f"Notebook 不存在：{notebook_id}")
    kind = access_control.scope_from_notebook(db, nb).kind
    if kind != access_control.SCOPE_ADMIN:
        raise ReconciliationError(
            f"Notebook 不是 admin scope（实际 {kind}），拒绝扩大权限风险"
        )
    return nb


def _load_manifest(manifest_path: str | None) -> dict:
    if manifest_path:
        p = Path(manifest_path).expanduser().resolve()
        if not p.exists() or p.is_dir():
            raise ReconciliationError(f"--manifest 文件不存在：{p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReconciliationError(f"无法读取 manifest：{p}") from exc
    else:
        data = DingTalkLocalStorage().read_manifest()
    if not isinstance(data, dict) or not isinstance(data.get("documents"), list):
        raise ReconciliationError("manifest 格式不正确（缺 documents 列表）")
    return data


def _existing_mapping(db, connection_id: str, space_id: str, folder_path: str):
    """按 (connection_id, path_namespace=space_id, folder_path) 精确查找根目录映射。"""
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
        raise ReconciliationError(
            f"根目录映射存在多条重复（{len(rows)}），拒绝猜测"
        )
    return rows[0] if rows else None


def _existing_source_item(db, connection_id: str, external_id: str):
    """按 (connection_id, external_id) 精确查找既有 SourceItem。"""
    rows = (
        db.query(SourceItem)
        .filter(
            SourceItem.connection_id == connection_id,
            SourceItem.external_id == external_id,
        )
        .all()
    )
    if len(rows) > 1:
        raise ReconciliationError(
            f"SourceItem 存在多条重复（connection={connection_id} external={external_id}）"
        )
    return rows[0] if rows else None


def _item_matches(existing: SourceItem, expected: dict) -> bool:
    """既有 SourceItem 与目标完全一致（connection/page/path/space/state/url/hash）。"""
    return (
        existing.page_id == expected["page_id"]
        and existing.state == expected["state"]
        and (existing.source_path or "") == expected["source_path"]
        and (existing.acl_json or "") == expected["acl_json"]
        and (existing.source_url or "") == expected["source_url"]
        and (existing.external_version or "") == expected["external_version"]
        and (existing.content_hash or "") == expected["content_hash"]
    )


def reconcile(
    *,
    db_path: str,
    space_id: str,
    connection_id: str,
    notebook_id: str,
    create_root_mapping: bool,
    apply: bool,
    manifest_path: str | None,
) -> dict:
    abs_db = _resolve_abs_db(db_path)
    print(f"[info] 数据库绝对路径（解析后）：{abs_db}")

    if not space_id:
        raise ReconciliationError("--space-id 必填")
    if not notebook_id:
        raise ReconciliationError("--notebook-id 必填")
    if not connection_id:
        raise ReconciliationError("--connection-id 必填")

    engine = get_engine(f"sqlite:///{abs_db.as_posix()}")
    db = sessionmaker(bind=engine)()

    result = {
        "apply": apply,
        "database": str(abs_db),
        "manifest_documents": 0,
        "imported_documents": 0,
        "not_imported": 0,
        "validated_pages": 0,
        "inserted_source_items": 0,
        "already_present": 0,
        "skipped": 0,
        "conflicts": 0,
        "mapping_created": 0,
        "mapping_existing": 0,
        "connection_id": connection_id,
        "notebook_id": notebook_id,
        "space_id": space_id,
        "error": None,
    }

    try:
        # ---- 1. 校验 Connection / Notebook（只读） ----
        _validate_source_connection(db, connection_id)
        _validate_notebook(db, notebook_id)

        # ---- 2. 加载 manifest（只读） ----
        manifest = _load_manifest(manifest_path)
        docs = manifest.get("documents", [])
        result["manifest_documents"] = len(docs)

        # ---- 3. 根目录映射：构造/校验（进入事务；dry-run 稍后回滚） ----
        folder_path = ""
        mapping = _existing_mapping(db, connection_id, space_id, folder_path)
        if mapping is not None:
            if mapping.notebook_id != notebook_id:
                raise ReconciliationError(
                    f"根目录映射已指向其他 Notebook（{mapping.notebook_id}），fail closed"
                )
            result["mapping_existing"] = 1
        elif create_root_mapping:
            # 计划创建根映射：add+flush 进事务，使 resolve_target_notebook_id
            # 的真实匹配可见；dry-run 最后 rollback，零写入。
            db.add(SourcePathMapping(
                id=str(uuid.uuid4()),
                connection_id=connection_id,
                path_namespace=space_id,
                folder_path=folder_path,
                notebook_id=notebook_id,
                created_by="reconcile",
            ))
            db.flush()
            result["mapping_created"] = 1
        # 否则（无根映射且未授权创建）：不 add，resolve 将返回 None → 逐条 fail closed。

        # ---- 4. 逐条校验 manifest 条目（真实 resolve，只读） ----
        to_insert: list[dict] = []
        for doc in docs:
            document_id = str(doc.get("document_id") or "").strip()
            rag_page_id = str(doc.get("rag_page_id") or "").strip()
            entry_space = str(doc.get("space_id") or "").strip()

            if not document_id or not rag_page_id:
                result["not_imported"] += 1
                continue

            result["imported_documents"] += 1

            # space_id 必须精确等于指定 space_id
            if entry_space != space_id:
                result["conflicts"] += 1
                raise ReconciliationError(
                    f"条目 space_id 不符（{entry_space!r} != {space_id!r}），fail closed"
                )

            # Page 必须存在 + source_type == dingtalk
            page = db.get(Page, rag_page_id)
            if page is None:
                result["conflicts"] += 1
                raise ReconciliationError(
                    f"rag_page_id 对应 Page 不存在：{rag_page_id}"
                )
            if page.source_type != "dingtalk":
                result["conflicts"] += 1
                raise ReconciliationError(
                    f"Page.source_type 非 dingtalk：{page.source_type}"
                )

            # 真实 resolve 目标 Notebook（根映射已在事务内 flush，可被匹配）
            resolved = resolve_target_notebook_id_for_entry(db, {
                "space_id": entry_space,
                "dingtalk_path": str(doc.get("dingtalk_path") or ""),
            })
            if resolved != notebook_id:
                result["conflicts"] += 1
                raise ReconciliationError(
                    f"resolve 目标 != 指定 admin Notebook（resolved={resolved!r}）"
                )

            # Page.notebook_id 必须与映射结果完全一致
            if page.notebook_id != notebook_id:
                result["conflicts"] += 1
                raise ReconciliationError(
                    f"Page.notebook_id（{page.notebook_id}）!= 映射结果（{notebook_id}）"
                )

            # 既有 SourceItem：完全一致 → already_present；冲突 → fail closed
            existing = _existing_source_item(db, connection_id, document_id)
            source_path = str(doc.get("dingtalk_path") or "")
            acl_json = json.dumps({"space_id": space_id}, ensure_ascii=False)
            expected = {
                "page_id": rag_page_id,
                "state": "active",
                "source_path": source_path,
                "acl_json": acl_json,
                "source_url": str(doc.get("source_url") or ""),
                "external_version": str(doc.get("source_file_hash") or doc.get("markdown_hash") or ""),
                "content_hash": str(doc.get("markdown_hash") or ""),
            }
            if existing is not None:
                if _item_matches(existing, expected):
                    result["already_present"] += 1
                else:
                    result["conflicts"] += 1
                    raise ReconciliationError(
                        f"既有 SourceItem 冲突（external_id={document_id}），fail closed"
                    )
                continue

            result["validated_pages"] += 1
            to_insert.append({
                "external_id": document_id,
                "page_id": rag_page_id,
                "source_path": source_path,
                "acl_json": acl_json,
                "source_url": expected["source_url"],
                "external_version": expected["external_version"],
                "content_hash": expected["content_hash"],
                "source_updated_at": _parse_dt(doc.get("source_updated_at")),
            })

        # ---- 5. 提交 / 回滚（单事务原子） ----
        if apply:
            try:
                for item in to_insert:
                    db.add(SourceItem(
                        id=str(uuid.uuid4()),
                        connection_id=connection_id,
                        external_id=item["external_id"],
                        external_version=item["external_version"],
                        content_hash=item["content_hash"],
                        source_url=item["source_url"],
                        source_path=item["source_path"],
                        page_id=item["page_id"],
                        state="active",
                        acl_json=item["acl_json"],
                        source_updated_at=item["source_updated_at"],
                        last_synced_at=datetime.now(),
                    ))
                db.commit()
                result["inserted_source_items"] = len(to_insert)
            except Exception:
                db.rollback()
                raise
        else:
            # dry-run：回滚已 flush 的 planned_mapping，零写入。
            result["inserted_source_items"] = len(to_insert)
            db.rollback()

        return result
    except Exception as exc:
        db.rollback()
        result["error"] = str(exc)
        return result
    finally:
        db.close()
        engine.dispose()


def main(argv=None) -> int:
    # 仅 CLI 入口重定向 stdout（避免模块 import 时污染 pytest 捕获机制）。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="钉钉历史 SourceItem reconciliation（默认 dry-run，零写入）"
    )
    parser.add_argument("--db", required=True, help="SQLite 数据库文件绝对路径")
    parser.add_argument("--space-id", required=True, help="钉钉 space_id")
    parser.add_argument("--connection-id", required=True, help="现有钉钉 SourceConnection.id")
    parser.add_argument("--notebook-id", required=True, help="现有 admin Notebook.id")
    parser.add_argument("--create-root-mapping", action="store_true",
                        help="允许创建根目录映射（space_id + folder_path=''）")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    parser.add_argument("--manifest", default=None, help="manifest 路径（缺省用 DingTalkLocalStorage）")
    args = parser.parse_args(argv)

    result = reconcile(
        db_path=args.db,
        space_id=args.space_id,
        connection_id=args.connection_id,
        notebook_id=args.notebook_id,
        create_root_mapping=args.create_root_mapping,
        apply=args.apply,
        manifest_path=args.manifest,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["error"] is not None:
        return 1
    if result["conflicts"] != 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
