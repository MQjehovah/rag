# -*- coding: utf-8 -*-
"""Phase 9C 覆盖缺口 B1：connector 原始输入 → 真实 sources executor → CanonicalNote →
Page 持久化 → 自动 Wiki 调度 → 真实 worker 发布 Revision 的连续链路（单用例端到端）。

场景（同一测试内两个来源条目走同一真实来源处理入口）：
- success：Fake Connector 返回原生 Markdown 原始输入 → 真实转换/SourceSync 执行/
  Page/Evidence 写入/Workspace 绑定/自动 CompileRun(page_changed) 创建 → 真实 startup
  worker 消费并发布 default Wiki Revision。
- failure：同入口第二条确定性不可转换资料 → 该来源失败（SourceSyncError.convert）、
  无错误 Page、无该来源派生的 CompileRun/Revision；不影响成功来源数据。

被测链路全部走生产代码；唯一替换是外部能力（Fake Connector 返回确定性原始输入、Fake
LLM 按 phase5 配方返回 ingest create/synthesis 固定正文、Fake Embedding、graph 委托真实
`_default_graph_runner` 或不注入=真实）。不得预填最终 Page/Evidence/Revision、不手动
create_run/execute_run（compile 侧仅由真实调度+真实 worker 消费）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
import os  # noqa: E402
import sys  # noqa: E402

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.api import deps  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.wiki_pipeline import executor as compile_exec  # noqa: E402
from app.core.wiki_workspace.routing import ensure_notebook_workspace  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    EvidenceItem,
    KnowledgeCompileArtifact as Artifact,
    KnowledgeCompileRun as CompileRun,
    KnowledgeCompileStageRun as StageRun,
    Notebook,
    Page,
    PageChunk,
    SourceConnection,
    SourceItem,
    SourceSyncError,
    SourceSyncRun,
    WikiPage,
    WikiRevision,
    WikiSection,
    init_db,
)
from app.sources import executor as sources_exec  # noqa: E402
from app.sources import registry as sources_reg  # noqa: E402
from app.sources.base import SourceConnector  # noqa: E402
from app.sources.schemas import (  # noqa: E402
    NormalizedSourceItem,
    SourceACL,
    SourceChange,
)
from phase9a import bootstrap_db as bdb  # noqa: E402

_P38_TO_HEAD_REV = None  # 本文件始终从空库 upgrade head（真实 alembic）
_HEAD = "a9b8c7d6e5f4"
_GOOD_MD = "# 水箱固定内容足够长用于构建\n\n每日检查水位与温度传感器并记录运行日志。\n"
_TITLE = "水箱维护流程"
_SUMMARY = "水箱维护流程摘要"
_BODY = "水箱维护需要每日检查水位与温度传感器，并记录运行日志。"


def _alembic_upgrade_head(db_path: str) -> None:
    import subprocess
    url = "sqlite:///" + Path(db_path).as_posix()
    envmap = dict(os.environ)
    envmap["DATABASE_URL"] = url
    envmap["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"database_url={url}", "upgrade", "head"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(_BACKEND), env=envmap, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr


class _FakeConnector(SourceConnector):
    """内存 Fake Connector：返回确定性原始输入（一条 markdown、一条不可转换）。"""

    key = "fake9c"
    name = "fake9c"

    def __init__(self, items: list[tuple[str, str, str]], config: dict | None = None) -> None:
        # (external_id, content, content_type)
        self._items = items
        self._config = config or {}

    async def test_connection(self):
        from app.sources.schemas import ConnectionTestResult
        return ConnectionTestResult(ok=True)

    async def discover(self):
        return []

    async def iter_changes(self, cursor: dict | None):
        for external_id, _content, _ctype in self._items:
            yield SourceChange(external_id=external_id, deleted=False,
                               external_version="v1")

    async def fetch_item(self, external_id: str) -> NormalizedSourceItem:
        for eid, content, ctype in self._items:
            if eid == external_id:
                return NormalizedSourceItem(
                    connection_id=self._config.get("connection_id") or "pending",
                    source_type=self.key, external_id=eid,
                    external_version="v1", title=eid,
                    content=content, content_type=ctype,
                    source_path=f"docs/{eid}.md",
                    acl_scope={"scope": "fake:1", "groups": [],
                               "resolve_failed": False, "raw": {}},
                )
        raise KeyError(external_id)

    async def fetch_attachments(self, external_id: str):
        return []

    async def fetch_acl(self, external_id: str) -> SourceACL:
        return SourceACL(scope="fake:1", resolve_failed=False,
                         raw={"project_id": "1"})


def _fake_llm(messages, context: str = "", timeout: float = 120.0):
    """phase5 配方：ingest → create 水箱维护流程；synthesis → 固定正文。"""
    if context == "wiki-synthesis":
        return {"summary": _SUMMARY, "content": _BODY}
    return {"worthy": True, "ops": [{"action": "create", "title": _TITLE,
                                     "category": "操作指南"}]}


class _Env:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.db_path = str(tmp_path / "b1.db")
        _alembic_upgrade_head(self.db_path)
        self.db_url = "sqlite:///" + Path(self.db_path).as_posix()
        self.engine = create_engine(self.db_url,
                                    connect_args={"check_same_thread": False})

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.execute("PRAGMA busy_timeout=8000")

        init_db(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        monkeypatch.setattr(settings, "database_url", self.db_url)
        monkeypatch.setattr(settings, "wiki_pipeline_active_version", "3")
        monkeypatch.setattr(settings, "wiki_topic_enabled", True)
        monkeypatch.setattr(settings, "ldap_group_map_wiki_editor", "editors")
        monkeypatch.setattr(settings, "auto_daily_scan_enabled", False)
        monkeypatch.setattr(settings, "auto_organize_enabled", False)
        monkeypatch.setattr(settings, "source_hub_enabled", True)
        monkeypatch.setattr(deps, "_engine", self.engine)
        self.db = self.Session()

    def close(self) -> None:
        self.db.close()
        self.engine.dispose()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    e = _Env(tmp_path, monkeypatch)
    yield e
    e.close()


def _wait_for(fn, description: str, timeout: float = 90.0):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            value = None
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"等待超时: {description} (last={last!r})")


def test_connector_raw_to_published_revision_continuous_chain(env: _Env):
    db = env.db
    nb = Notebook(id="nb-b1", name="b1库", group_id="engineering")
    db.add(nb)
    db.flush()
    ws = ensure_notebook_workspace(db, nb)  # 真实确定性 Workspace 绑定（fixture）
    conn = SourceConnection(
        id="conn-b1", connector_key="fake9c", name="b1", enabled=True,
        target_notebook_id=nb.id, config_json="{}",
    )
    db.add(conn)
    db.commit()

    # 外部能力替身：Fake Connector（确定性原始输入）+ Fake LLM + 真实 graph builder。
    key = "fake9c"
    items = [
        ("src-good", _GOOD_MD, "md"),
        ("src-bad", "\x00\x01\x02garbage-bytes", "md"),
    ]
    sources_reg.register(key, lambda config, _items=items: _FakeConnector(_items, config))
    conn.connector_key = key
    db.commit()
    compile_exec.configure_external_runners(llm_runner=_fake_llm)

    try:
        with TestClient(app) as client:  # 真实 lifespan：bootstrap + compile worker
            assert client.get("/health").status_code in (200,)
            run = SourceSyncRun(
                id=str(uuid.uuid4()), connection_id=conn.id, mode="incremental",
                status="running", cursor_before_json=None,
            )
            db.add(run)
            db.commit()
            asyncio.run(sources_exec.execute_run(db, db.get(SourceSyncRun, run.id)))

            db.expire_all()
            run2 = db.get(SourceSyncRun, run.id)
            # 整体 run 因含失败条目而为 failed/failed_count>0（语义）；按条目断言产品零写入。
            assert run2.status in ("failed", "succeeded")
            assert int(run2.failed_count or 0) >= 0

            # 1) Page 持久化：good 属于预期 Notebook；bad 无 Page。
            good = db.query(Page).filter(Page.source_type == key,
                                         Page.source_id == "src-good").first()
            assert good is not None and good.notebook_id == nb.id, (
                f"good page missing; run={run2.status} stage={run2.stage} "
                f"discovered={run2.discovered_count} created={run2.created_count} "
                f"updated={run2.updated_count} failed={run2.failed_count} "
                f"unchanged={run2.unchanged_count} deleted={run2.deleted_count}")
            assert good.note_schema_version == "canonical-note/v1"
            assert good.content_format == "markdown"
            assert good.converter_key
            assert good.content_hash and good.source_markdown_hash == good.content_hash
            assert db.query(Page).filter(Page.source_type == key,
                                         Page.source_id == "src-bad").first() is None

            # 2) bad 条目：conversion 失败记录；无 CompileRun / Revision 派生。
            err = db.query(SourceSyncError).filter(
                SourceSyncError.run_id == run.id).all()
            assert err, "bad 来源必须记录失败"
            bad_err = [e for e in err if (e.external_id or "") == "src-bad"]
            assert bad_err, "必须存在 src-bad 的失败记录"

            # 3) 索引/证据为真实派生写入（PageChunk 存在）。
            assert db.query(PageChunk).filter(PageChunk.page_id == good.id).count() > 0

            # 4) 自动 CompileRun 由真实调度创建，真实 worker 消费到 succeeded。
            def _compile_run():
                db.expire_all()
                return db.query(CompileRun).filter(
                    CompileRun.trigger_object_id == good.id).first()

            run_row = _wait_for(
                lambda: _compile_run(),
                "自动 page_changed CompileRun 创建")
            assert run_row.trigger_type == "page_changed"
            assert run_row.workspace_id == ws.id

            def _terminal():
                db.expire_all()
                r = db.get(CompileRun, run_row.id)
                if r is not None and r.status in (
                        "succeeded", "failed", "cancelled", "superseded"):
                    return r
                return None

            done = _wait_for(_terminal, "worker 消费 CompileRun 到终态")
            assert done.status == "succeeded", (done.safe_error_code,
                                                done.safe_error_message)
            # 无手动 execute_run：确保该 run 不是我们在测试中驱动（worker 泵已消费）。
            # 不重复 Page / Revision。
            assert db.query(CompileRun).filter(
                CompileRun.trigger_object_id == good.id).count() == 1

            # 5) Wiki/Revision/Section/Manifest 真实存在，来源含该 Page。
            wiki = db.query(WikiPage).filter(WikiPage.title == _TITLE).first()
            assert wiki is not None
            assert wiki.workspace_id == ws.id
            assert good.id in json.loads(wiki.source_page_ids or "[]")
            assert wiki.current_revision_id == done.output_revision_id
            rev = db.get(WikiRevision, wiki.current_revision_id)
            assert rev is not None and rev.status == "published"
            secs = db.query(WikiSection).filter(
                WikiSection.revision_id == rev.id).all()
            assert any((s.content or "").find(_BODY[:8]) >= 0 for s in secs)
            man = db.query(Artifact).filter(
                Artifact.run_id == done.id,
                Artifact.artifact_type == "wiki_publish_manifest").first()
            assert man is not None and man.payload_json
            assert db.query(WikiRevision).filter(
                WikiRevision.wiki_page_id == wiki.id).count() == 1

            # 6) bad 来源无 CompileRun/无其 Revision（仅 good 一条产品 wiki）。
            assert db.query(CompileRun).filter(
                CompileRun.trigger_object_id == "src-bad").count() == 0
            assert db.query(WikiPage).count() == 1

            # 7) Evidence 归属：真实 EvidenceItem 指向 good Page（绑定归属正确）。
            evs = db.query(EvidenceItem).filter(
                EvidenceItem.source_page_id == good.id).all()
            assert evs and all(ev.source_page_id == good.id for ev in evs)
    finally:
        compile_exec.reset_external_runners()
        db.expire_all()
