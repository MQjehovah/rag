import io
import sys
from types import SimpleNamespace

from openpyxl import Workbook

from app.core.dingtalk import DingTalkClient


def test_pdf_extraction_uses_pypdf_fallback(monkeypatch):
    class FakePage:
        def extract_text(self):
            return "第一页内容"

    class FakeReader:
        def __init__(self, _stream):
            self.pages = [FakePage()]

    monkeypatch.setitem(
        sys.modules,
        "pypdf",
        SimpleNamespace(PdfReader=FakeReader),
    )

    assert DingTalkClient._extract_pdf_text(b"%PDF-test") == "第一页内容"


def test_xlsx_conversion_produces_valid_markdown_table():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "设备清单"
    sheet.append(["名称", "数量"])
    sheet.append(["机器人", 2])

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()

    markdown = DingTalkClient._extract_text(buffer.getvalue(), "xlsx")

    assert "## 设备清单" in markdown
    assert "| 名称 | 数量 |" in markdown
    assert "| --- | --- |" in markdown
    assert "| 机器人 | 2 |" in markdown
