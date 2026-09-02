"""P1-BE-05：Markdown 表格解析器 + table Observation 生成测试。

解析器单测覆盖真实数据中的三种形态（完整表 / 仅数据行 / 破碎文本）；
脚本测试用临时库验证幂等、needs_review、asset_id=chunk_id 语义。
"""
from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.table_parser import is_separator, parse_markdown_tables, split_row
from app.models.database import Page, PageChunk, get_engine, init_db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "v3_table_observations.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("v3_table_observations", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 解析器单测 ----------

def test_split_row_keeps_empty_cells():
    """内部空单元格必须保留，否则列错位（'| a | | c |' 是 3 列）。"""
    assert split_row("| a | | c |") == ["a", "", "c"]
    assert split_row("|a|b|") == ["a", "b"]


def test_is_separator():
    assert is_separator("| --- | --- |")
    assert is_separator("|:---|---:|")
    assert not is_separator("| a | b |")
    assert not is_separator("无管道符 - 文本")


def test_parse_complete_table():
    """真实数据形态 (a)：完整表（含 <br> cell 与占位表头「列2」）。"""
    content = (
        "## 第 3 页 - Service Fee Definition\n"
        "| Product Repair complexity Description | 列2 | 列3 |\n"
        "| --- | --- | --- |\n"
        "| Titan 810<br>X-Rolling | High | Parts mounted on chassis |\n"
        "| SW50 | Low | Cover parts |\n"
    )
    tables = parse_markdown_tables(content)
    assert len(tables) == 1
    assert tables[0]["headers"] == ["Product Repair complexity Description", "列2", "列3"]
    assert tables[0]["rows"][0] == ["Titan 810<br>X-Rolling", "High", "Parts mounted on chassis"]
    assert len(tables[0]["rows"]) == 2


def test_parse_header_only_rows_dropped():
    """有表头+分隔行但无数据行 → 不产出表（规范结构要求 ≥1 数据行）。"""
    content = "| a | b |\n| --- | --- |\n"
    assert parse_markdown_tables(content) == []


def test_parse_pipe_rows_without_header():
    """真实数据形态 (b)：仅数据行有管道符、无表头/分隔行 → 解析不出表。"""
    content = (
        "| 16 | 1300204171 | 加 水 口 旋 转 轴 | 1 | AXISOFROTATION |\n"
        "| 17 | 1300204172 | 轴 承 | 2 | BEARING |\n"
    )
    assert parse_markdown_tables(content) == []


def test_parse_broken_ocr_text():
    """真实数据形态 (c)：破碎 OCR（管道符断裂、正文行混入）→ 解析不出表。"""
    content = (
        "| --------- | --- |\n"
        "加 水 口 旋 转 轴 1\n"
        "BEARING 2\n"
    )
    assert parse_markdown_tables(content) == []


def test_parse_multiple_tables_and_interruption():
    """两个表被正文行分隔 → 各自解析；分隔行也中断数据行。"""
    content = (
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        "中间正文行\n"
        "| c | d |\n| --- | --- |\n| 3 | 4 |\n| 5 | 6 |\n"
    )
    tables = parse_markdown_tables(content)
    assert len(tables) == 2
    assert tables[0]["rows"] == [["1", "2"]]
    assert tables[1]["rows"] == [["3", "4"], ["5", "6"]]


def test_parse_empty_content():
    assert parse_markdown_tables("") == []
    assert parse_markdown_tables(None) == []


# ---------- 脚本集成测试 ----------

def _make_table_chunk(db, content: str) -> str:
    page = Page(id=str(uuid.uuid4()), title="t", content="c")
    db.add(page)
    db.flush()
    cid = str(uuid.uuid4())
    db.add(PageChunk(
        id=cid, page_id=page.id, chunk_index=0,
        content=content, content_type="table", page_number=1,
    ))
    db.commit()
    return cid


@pytest.fixture()
def table_db(tmp_path, monkeypatch):
    """临时库：1 个可解析 table chunk + 1 个破碎 chunk + 1 个非 table chunk。"""
    url = f"sqlite:///{(tmp_path / 'table_test.db').as_posix()}"
    engine = get_engine(url)
    init_db(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    good_id = _make_table_chunk(db, (
        "| 零件 | 数量 |\n| --- | --- |\n| 旋 转轴 | 1 |\n| 轴 承 | 2 |\n"
    ))
    broken_id = _make_table_chunk(db, "| --------- | --- |\n无数据行正文\n")
    _make_table_chunk(db, "普通文本 chunk")  # content_type 不符，不应被处理
    # 修正第三个 chunk 的 content_type（_make_table_chunk 固定建 table 类型）
    db.query(PageChunk).filter(PageChunk.content == "普通文本 chunk").update(
        {"content_type": "text"})
    db.commit()
    db.close()
    engine.dispose()
    monkeypatch.setattr("app.config.settings.database_url", url)
    return url, good_id, broken_id


def _observations(url):
    engine = get_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT asset_id, content, extraction_method, confidence, "
            "needs_review, content_hash FROM asset_observations "
            "WHERE observation_type = 'table'"
        )).fetchall()
    engine.dispose()
    return {r[0]: r for r in rows}


def test_table_writes_observation(table_db):
    url, good_id, broken_id = table_db
    mod = _load_script()
    assert mod.main() == 0

    rows = _observations(url)
    assert set(rows) == {good_id, broken_id}  # 非 table chunk 不出现

    good = rows[good_id]
    payload = json.loads(good[1])
    assert payload["tables"] == [{
        "headers": ["零件", "数量"],
        "rows": [["旋 转轴", "1"], ["轴 承", "2"]],
    }]
    assert good[2] == "parser"
    assert good[3] == 1.0
    assert good[4] == 0  # 可解析 → 不需复核

    broken = rows[broken_id]
    assert json.loads(broken[1])["tables"] == []
    assert broken[3] == 0.0
    assert broken[4] == 1  # 破碎 → needs_review


def test_table_idempotent(table_db):
    url, good_id, broken_id = table_db
    mod = _load_script()
    assert mod.main() == 0
    assert mod.main() == 0
    assert len(_observations(url)) == 2


def test_table_dry_run_no_write(table_db):
    url, _good, _broken = table_db
    mod = _load_script()
    import sys
    sys.argv.append("--dry-run")
    try:
        assert mod.main() == 0
    finally:
        sys.argv.remove("--dry-run")
    assert len(_observations(url)) == 0
