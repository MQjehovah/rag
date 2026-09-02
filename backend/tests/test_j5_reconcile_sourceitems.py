"""Phase J-5 定向测试：钉钉历史 SourceItem reconciliation 脚本。

全程临时 SQLite 文件库 + 临时 manifest，绝不触碰真实库。
覆盖 reconcile_dingtalk_sourceitems.py 的 15 项安全规则。
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    SourcePathMapping,
    Notebook,
    Page,
    SourceConnection,
    SourceItem,
    init_db,
)
from scripts.reconcile_dingtalk_sourceitems import reconcile

SPACE = "space-root-1"
CONN_ID = "conn-dingtalk-1"
NB_ADMIN = "nb-admin-1"


def _make_db(tmp_path):
    """临时文件库 + PRAGMA foreign_keys=ON + 完整 ORM schema。"""
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine, db_path


def _seed_base(engine, *, notebook_group="__local_admin__", source_type="dingtalk",
               page_notebook=None):
    """基础种子：admin Notebook + dingtalk Connection；返回 (nb_id, conn_id)。"""
    session = sessionmaker(bind=engine)()
    nb = Notebook(id=NB_ADMIN, name="钉钉知识库", group_id=notebook_group)
    conn = SourceConnection(id=CONN_ID, connector_key="dingtalk", name="钉钉知识库", enabled=True)
    session.add_all([nb, conn])
    session.commit()
    session.close()
    return NB_ADMIN, CONN_ID


def _make_manifest(tmp_path, docs):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"documents": docs}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _page(session, page_id, *, source_type="dingtalk", notebook_id=NB_ADMIN):
    session.add(Page(id=page_id, title="p", notebook_id=notebook_id,
                     source_type=source_type, source_id=page_id))
    session.flush()


def _doc(document_id, rag_page_id=None, space_id=SPACE, dingtalk_path="a/b.pdf",
         source_url="https://x", markdown_hash="mh", source_file_hash="sf"):
    d = {
        "document_id": document_id,
        "space_id": space_id,
        "dingtalk_path": dingtalk_path,
        "source_url": source_url,
        "markdown_hash": markdown_hash,
        "source_file_hash": source_file_hash,
    }
    if rag_page_id is not None:
        d["rag_page_id"] = rag_page_id
    return d


def _run(tmp_path, db_path, *, manifest, apply=False, **kw):
    return reconcile(
        db_path=str(db_path),
        space_id=SPACE,
        connection_id=CONN_ID,
        notebook_id=NB_ADMIN,
        create_root_mapping=True,
        apply=apply,
        manifest_path=manifest,
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. dry-run 零写入
# ---------------------------------------------------------------------------

def test_dry_run_zero_write(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    for i in range(3):
        _page(session, f"pg{i}", source_type="dingtalk", notebook_id=NB_ADMIN)
    session.commit(); session.close()

    manifest = _make_manifest(tmp_path, [_doc(f"d{i}", f"pg{i}") for i in range(3)])
    r = _run(tmp_path, db_path, manifest=manifest, apply=False)

    assert r["error"] is None
    assert r["inserted_source_items"] == 3

    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    assert session.query(SourcePathMapping).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 2. 144 有 page_id + 20 无 page_id，只计划 144 条
# ---------------------------------------------------------------------------

def test_144_plus_20_only_plans_144(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    for i in range(144):
        _page(session, f"pg{i}")
    session.commit(); session.close()

    docs = [_doc(f"d{i}", f"pg{i}") for i in range(144)]
    docs += [_doc(f"n{i}", None) for i in range(20)]
    manifest = _make_manifest(tmp_path, docs)
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)

    assert r["error"] is None
    assert r["manifest_documents"] == 164
    assert r["imported_documents"] == 144
    assert r["not_imported"] == 20
    assert r["validated_pages"] == 144
    assert r["inserted_source_items"] == 144

    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 144
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 3. 无 rag_page_id 不创建 active SourceItem
# ---------------------------------------------------------------------------

def test_not_imported_creates_no_item(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    manifest = _make_manifest(tmp_path, [_doc("n1", None)])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is None
    assert r["not_imported"] == 1
    assert r["inserted_source_items"] == 0
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 4. Page 不存在 fail closed
# ---------------------------------------------------------------------------

def test_missing_page_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg_missing")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is not None
    assert "不存在" in r["error"]
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    assert session.query(SourcePathMapping).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 5. Page.source_type 非 dingtalk fail closed
# ---------------------------------------------------------------------------

def test_wrong_source_type_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    _page(session, "pg1", source_type="gitlab")
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is not None
    assert "非 dingtalk" in r["error"]
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 6. Page.notebook_id 与映射结果不一致 fail closed
# ---------------------------------------------------------------------------

def test_page_notebook_mismatch_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    session.add(Notebook(id="nb-other", name="其他库", group_id="engineering"))
    _page(session, "pg1", notebook_id="nb-other")
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is not None
    assert "notebook_id" in r["error"]
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 7. 根映射冲突 fail closed
# ---------------------------------------------------------------------------

def test_root_mapping_conflict_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    session.add(Notebook(id="nb-other", name="其他库", group_id="engineering"))
    _page(session, "pg1")
    # 预置一条指向其他 Notebook 的根映射
    session.add(SourcePathMapping(id=str(uuid.uuid4()), connection_id=CONN_ID, path_namespace=SPACE,
                                      folder_path="", notebook_id="nb-other"))
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is not None
    assert "其他 Notebook" in r["error"]
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 8. Notebook 非 admin scope fail closed
# ---------------------------------------------------------------------------

def test_notebook_not_admin_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    # group_id = 业务组（非 admin）
    _seed_base(engine, notebook_group="engineering")
    session = sessionmaker(bind=engine)()
    _page(session, "pg1")
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is not None
    assert "admin scope" in r["error"]
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 9. SourceConnection 不存在 / 非 dingtalk fail closed
# ---------------------------------------------------------------------------

def test_connection_missing_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    _page(session, "pg1")
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = reconcile(db_path=str(db_path), space_id=SPACE, connection_id="conn-missing",
                  notebook_id=NB_ADMIN, create_root_mapping=True, apply=True,
                  manifest_path=manifest)
    assert r["error"] is not None
    assert "SourceConnection 不存在" in r["error"]


def test_connection_not_dingtalk_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    session.add(SourceConnection(id="conn-gitlab", connector_key="gitlab", name="gitlab"))
    _page(session, "pg1")
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = reconcile(db_path=str(db_path), space_id=SPACE, connection_id="conn-gitlab",
                  notebook_id=NB_ADMIN, create_root_mapping=True, apply=True,
                  manifest_path=manifest)
    assert r["error"] is not None
    assert "非 dingtalk" in r["error"]


# ---------------------------------------------------------------------------
# 10. 已有完全一致 SourceItem 幂等
# ---------------------------------------------------------------------------

def test_existing_identical_is_idempotent(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    _page(session, "pg1")
    session.add(SourceItem(id=str(uuid.uuid4()), connection_id=CONN_ID,
                           external_id="d1", page_id="pg1", state="active",
                           source_path="a/b.pdf", acl_json=json.dumps({"space_id": SPACE}),
                           source_url="https://x", external_version="sf",
                           content_hash="mh"))
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is None
    assert r["already_present"] == 1
    assert r["inserted_source_items"] == 0
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 1
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 11. 已有冲突 SourceItem fail closed
# ---------------------------------------------------------------------------

def test_existing_conflict_fail_closed(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    _page(session, "pg1")
    session.add(SourceItem(id=str(uuid.uuid4()), connection_id=CONN_ID,
                           external_id="d1", page_id="pg-OTHER", state="active",
                           source_path="other.pdf", acl_json="{}"))
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is not None
    assert "冲突" in r["error"]
    session = sessionmaker(bind=engine)()
    # 仍只有原有 1 条，未新增
    assert session.query(SourceItem).count() == 1
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 12. 中途故障注入后映射和 SourceItem 全 rollback
# ---------------------------------------------------------------------------

def test_partial_failure_rolls_back(tmp_path, monkeypatch):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    for i in range(3):
        _page(session, f"pg{i}")
    session.commit(); session.close()
    docs = [_doc(f"d{i}", f"pg{i}") for i in range(2)]
    docs.append(_doc("d2", "pg_missing"))  # 第三条 Page 不存在 → 中途失败
    manifest = _make_manifest(tmp_path, docs)
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is not None
    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 0
    assert session.query(SourcePathMapping).count() == 0
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 13. 不修改 Page.notebook_id
# ---------------------------------------------------------------------------

def test_does_not_modify_page_notebook(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    _page(session, "pg1", notebook_id=NB_ADMIN)
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])
    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is None
    session = sessionmaker(bind=engine)()
    pg = session.get(Page, "pg1")
    assert pg.notebook_id == NB_ADMIN
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# 14. 不调用索引 / Wiki / 图谱 / 模型
# ---------------------------------------------------------------------------

def test_no_model_or_side_effects(tmp_path, monkeypatch):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    _page(session, "pg1")
    session.commit(); session.close()
    manifest = _make_manifest(tmp_path, [_doc("d1", "pg1")])

    called = {"index": False, "wiki": False, "graph": False, "llm": False}
    # 通过替换 import 副作用哨兵：本脚本只 import 有限模块，无索引/Wiki/图谱/模型调用路径。
    # 这里直接断言脚本 import 面不含这些写模块。
    import scripts.reconcile_dingtalk_sourceitems as mod
    src = open(mod.__file__, encoding="utf-8").read()
    for kw in ("rebuild_page_graph", "rebuild_wiki_graph", "call_llm", "EmbeddingService",
               "rerank", "embedding"):
        assert kw not in src, f"脚本不应引用 {kw}"

    r = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r["error"] is None
    assert r["inserted_source_items"] == 1


# ---------------------------------------------------------------------------
# 15. 运行两次 apply 后仍只有 144 条
# ---------------------------------------------------------------------------

def test_double_apply_stays_144(tmp_path):
    engine, db_path = _make_db(tmp_path)
    _seed_base(engine)
    session = sessionmaker(bind=engine)()
    for i in range(144):
        _page(session, f"pg{i}")
    session.commit(); session.close()
    docs = [_doc(f"d{i}", f"pg{i}") for i in range(144)]
    manifest = _make_manifest(tmp_path, docs)

    r1 = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r1["error"] is None
    assert r1["inserted_source_items"] == 144

    r2 = _run(tmp_path, db_path, manifest=manifest, apply=True)
    assert r2["error"] is None
    assert r2["inserted_source_items"] == 0
    assert r2["already_present"] == 144

    session = sessionmaker(bind=engine)()
    assert session.query(SourceItem).count() == 144
    assert session.query(SourcePathMapping).count() == 1
    session.close()
    engine.dispose()
