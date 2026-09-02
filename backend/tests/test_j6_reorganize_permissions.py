"""Phase J-6 定向测试：钉钉权限重组脚本（临时库，绝不触碰真实库）。

覆盖 reorganize_dingtalk_permissions.py 的 reorg 阶段核心规则。
wiki 阶段 dry-run 不调 LLM；cleanup 阶段复用 legacy_wiki_cleanup（已有测试）。
"""
from __future__ import annotations

import json

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
from scripts.reorganize_dingtalk_permissions import (
    SPACE_ID,
    CONNECTION_ID,
    ROOT_NOTEBOOK_ID,
    _stage_reorg,
)

DMS = "【DMS】交付服务部资料库"
FAE1 = "【内部】FAE工作管理"
FAE2 = "【内部】FAE知识库"


def _make_db(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    init_db(engine)
    return engine, db_path


def _seed(db):
    """种子：根 admin Notebook + dingtalk Connection + 根映射。"""
    root = Notebook(id=ROOT_NOTEBOOK_ID, name="钉钉知识库", group_id="__local_admin__")
    conn = SourceConnection(id=CONNECTION_ID, connector_key="dingtalk", name="钉钉知识库", enabled=True)
    db.add_all([root, conn])
    db.flush()  # 先落 Notebook/Connection，满足映射外键
    db.add(SourcePathMapping(id="m-root", connection_id=CONNECTION_ID, path_namespace=SPACE_ID, folder_path="", notebook_id=ROOT_NOTEBOOK_ID))
    db.flush()


def _add_page(db, page_id, path):
    db.add(Page(id=page_id, title="p", notebook_id=ROOT_NOTEBOOK_ID,
                source_type="dingtalk", source_id=page_id))
    db.add(SourceItem(id=f"si-{page_id}", connection_id=CONNECTION_ID,
                      external_id=f"ext-{page_id}", page_id=page_id, state="active",
                      source_path=path, acl_json=json.dumps({"space_id": SPACE_ID})))
    db.flush()


# ---------------------------------------------------------------------------

def test_dry_run_zero_write(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    _add_page(db, "pg1", f"{DMS}/a.pdf")
    _add_page(db, "pg2", f"{FAE1}/b.pdf")
    db.commit()

    r = _stage_reorg(db, apply=False)
    assert r["error"] is None
    # dry-run 不创建 Notebook / 映射 / 不 reassign
    assert db.query(Notebook).count() == 1
    assert db.query(SourcePathMapping).count() == 1
    pg1 = db.get(Page, "pg1")
    assert pg1.notebook_id == ROOT_NOTEBOOK_ID  # 未变
    db.close()
    engine.dispose()


def test_exact_distribution_92_52_0(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    # 92 DMS + 52 FAE
    for i in range(92):
        _add_page(db, f"pg-d{i}", f"{DMS}/doc{i}.pdf")
    for i in range(52):
        _add_page(db, f"pg-f{i}", f"{FAE2}/doc{i}.pdf")
    db.commit()

    r = _stage_reorg(db, apply=True)
    assert r["error"] is None

    dms_nb = db.query(Notebook).filter(Notebook.name == "钉钉知识库（DMS 交付资料）").one()
    fae_nb = db.query(Notebook).filter(Notebook.name == "FAE 内部知识库").one()
    root_nb = db.get(Notebook, ROOT_NOTEBOOK_ID)

    from collections import Counter
    c = Counter(db.query(Page.notebook_id).all())
    assert c[(dms_nb.id,)] == 92
    assert c[(fae_nb.id,)] == 52
    assert (c.get((root_nb.id,)) or 0) == 0

    # SourceItem 仍 144 active，无重复
    assert db.query(SourceItem).count() == 144
    assert db.query(SourceItem).filter(SourceItem.state == "active").count() == 144

    # 映射 4 条（1 根 + 3 顶级）
    assert db.query(SourcePathMapping).count() == 4
    db.close()
    engine.dispose()


def test_idempotent_second_apply(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    for i in range(3):
        _add_page(db, f"pg{i}", f"{DMS}/doc{i}.pdf")
    db.commit()

    r1 = _stage_reorg(db, apply=True)
    assert r1["error"] is None
    nb_count_after_first = db.query(Notebook).count()
    map_count_after_first = db.query(SourcePathMapping).count()

    r2 = _stage_reorg(db, apply=True)
    assert r2["error"] is None
    assert db.query(Notebook).count() == nb_count_after_first  # 不再新建
    assert db.query(SourcePathMapping).count() == map_count_after_first
    assert r2["reassign"]["reassigned"] == 0  # 已归属，无需再改
    db.close()
    engine.dispose()


def test_mapping_conflict_fail_closed(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    _add_page(db, "pg1", f"{DMS}/a.pdf")
    # 预置一个指向其他 Notebook 的 DMS 顶级映射
    other = Notebook(id="nb-other", name="其他", group_id="__local_admin__")
    db.add(other)
    db.flush()
    db.add(SourcePathMapping(id="m-dms", connection_id=CONNECTION_ID, path_namespace=SPACE_ID, folder_path=DMS, notebook_id="nb-other"))
    db.commit()

    r = _stage_reorg(db, apply=True)
    assert r["error"] is not None
    assert "其他" in r["error"]
    # 不产生部分写入：映射仍 2 条（根 + 冲突映射），无新 Notebook
    assert db.query(Notebook).count() == 2  # root + other
    db.close()
    engine.dispose()


def test_notebook_conflict_fail_closed(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    _add_page(db, "pg1", f"{DMS}/a.pdf")
    # 预置同名 Notebook 但非 admin scope
    db.add(Notebook(id="nb-dms", name="钉钉知识库（DMS 交付资料）", group_id="engineering"))
    db.commit()

    r = _stage_reorg(db, apply=True)
    assert r["error"] is not None
    db.close()
    engine.dispose()


def test_duplicate_notebook_name_fail_closed(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    _add_page(db, "pg1", f"{DMS}/a.pdf")
    db.add(Notebook(id="nb-dms-1", name="钉钉知识库（DMS 交付资料）", group_id="__local_admin__"))
    db.add(Notebook(id="nb-dms-2", name="钉钉知识库（DMS 交付资料）", group_id="__local_admin__"))
    db.commit()

    r = _stage_reorg(db, apply=True)
    assert r["error"] is not None
    assert "重复" in r["error"]
    db.close()
    engine.dispose()


def test_missing_root_mapping_fail_closed(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    root = Notebook(id=ROOT_NOTEBOOK_ID, name="钉钉知识库", group_id="__local_admin__")
    conn = SourceConnection(id=CONNECTION_ID, connector_key="dingtalk", name="钉钉知识库", enabled=True)
    db.add_all([root, conn])
    # 不建根映射
    _add_page(db, "pg1", f"{DMS}/a.pdf")
    db.commit()

    r = _stage_reorg(db, apply=True)
    assert r["error"] is not None
    assert "根目录映射缺失" in r["error"]
    db.close()
    engine.dispose()


def test_active_item_missing_page_fail_closed(tmp_path):
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    # active SourceItem 关联不存在的 Page
    db.add(SourceItem(id="si-orphan", connection_id=CONNECTION_ID,
                      external_id="ext-orphan", page_id="pg-missing", state="active",
                      source_path=f"{DMS}/x.pdf", acl_json=json.dumps({"space_id": SPACE_ID})))
    db.commit()

    r = _stage_reorg(db, apply=True)
    assert r["error"] is not None
    assert "不存在" in r["error"]
    db.close()
    engine.dispose()


def test_unmapped_path_falls_back_to_root(tmp_path):
    """未匹配任何顶级映射的路径 → 回落根映射（原 admin Notebook）。"""
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    _add_page(db, "pg-unknown", "其他未知目录/x.pdf")
    db.commit()

    r = _stage_reorg(db, apply=True)
    assert r["error"] is None
    pg = db.get(Page, "pg-unknown")
    assert pg.notebook_id == ROOT_NOTEBOOK_ID  # 回落根映射
    db.close()
    engine.dispose()


def test_result_json_roundtrip(tmp_path):
    """reorg 结果可序列化且不含 error。"""
    engine, _ = _make_db(tmp_path)
    db = sessionmaker(bind=engine)()
    _seed(db)
    _add_page(db, "pg1", f"{DMS}/a.pdf")
    db.commit()
    r = _stage_reorg(db, apply=True)
    assert r["error"] is None
    s = json.dumps(r, ensure_ascii=False, default=str)
    assert "error" in s
    db.close()
    engine.dispose()
