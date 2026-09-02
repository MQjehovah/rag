"""T1.1 盘点脚本的纯函数单元测试。

只测统计逻辑（不依赖运行中的后端）：
- _infer_extension：扩展名识别
- _bucket_updated：时间桶分配
- build_report：报告生成
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "inventory.py"


def _load_inventory_module():
    spec = importlib.util.spec_from_file_location("inventory_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["inventory_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def inventory():
    return _load_inventory_module()


def test_infer_extension_from_source_path(inventory):
    page = {"source_path": "政策/收费标准.pdf", "title": "收费标准"}
    assert inventory._infer_extension(page) == "pdf"


def test_infer_extension_falls_back_to_title(inventory):
    page = {"source_path": None, "title": "TITAN810 部署手册.docx"}
    assert inventory._infer_extension(page) == "docx"


def test_infer_extension_unknown_when_no_signal(inventory):
    page = {"source_path": "", "title": "无标题笔记"}
    assert inventory._infer_extension(page) == "无扩展名"


def test_bucket_updated_within_7_days(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    recent = (now - timedelta(days=3)).isoformat()
    assert inventory._bucket_updated(recent, now) == "近 7 天"


def test_bucket_updated_30_days(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    recent = (now - timedelta(days=20)).isoformat()
    assert inventory._bucket_updated(recent, now) == "近 30 天"


def test_bucket_updated_90_days(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    recent = (now - timedelta(days=60)).isoformat()
    assert inventory._bucket_updated(recent, now) == "近 90 天"


def test_bucket_updated_older(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    recent = (now - timedelta(days=200)).isoformat()
    assert inventory._bucket_updated(recent, now) == "更早"


def test_bucket_updated_invalid(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    assert inventory._bucket_updated("not-a-date", now) == "未知时间"


def test_build_report_contains_all_sections(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    notebooks = [
        {"id": "nb-1", "name": "钉钉知识库"},
        {"id": "nb-2", "name": "技术备忘"},
    ]
    pages = [
        {
            "id": "p1",
            "title": "TITAN810 部署.pdf",
            "notebook_id": "nb-1",
            "source_type": "dingtalk",
            "source_path": "技术/TITAN810 部署.pdf",
            "index_status": "current",
            "updated_at": (now - timedelta(days=5)).isoformat(),
        },
        {
            "id": "p2",
            "title": "E102 故障排查.docx",
            "notebook_id": "nb-1",
            "source_type": "dingtalk",
            "source_path": "运维/E102.docx",
            "index_status": "stale",
            "updated_at": (now - timedelta(days=40)).isoformat(),
        },
        {
            "id": "p3",
            "title": "随手记录",
            "notebook_id": None,
            "source_type": None,
            "source_path": None,
            "index_status": "missing",
            "updated_at": (now - timedelta(days=400)).isoformat(),
        },
    ]
    report = inventory.build_report(notebooks, pages, now)

    assert "# 知识库盘点报告" in report
    assert "## 按 Notebook 分布" in report
    assert "## 按来源(source_type)分布" in report
    assert "## 按文件扩展名分布" in report
    assert "## 按索引状态分布" in report
    assert "## 按更新时间分布" in report
    assert "钉钉知识库" in report
    assert "pdf" in report
    assert "docx" in report
    assert "stale" in report


def test_build_report_empty_knowledge_base_warns(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    report = inventory.build_report([], [], now)
    assert "知识库当前为空" in report


def test_build_report_flags_high_stale_ratio(inventory):
    now = datetime(2026, 8, 10, 12, 0, 0)
    pages = [
        {"title": f"p{i}", "notebook_id": None, "index_status": "stale", "updated_at": now.isoformat()}
        for i in range(10)
    ]
    report = inventory.build_report([], pages, now)
    assert "stale 占比 100.0%" in report
