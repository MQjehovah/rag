"""P0 文本归一化与实体抽取规则测试（不依赖 GLM）。

实体抽取由旧 card_graph.py 迁移至 V4 page_graph.py（Page/Chunk/Evidence 驱动）。
"""
from __future__ import annotations

from app.core.text_normalization import normalize_text
from app.core.knowledge_compiler_v3.page_graph import _extract_entities as _extract_entities_from_text


def test_normalize_text_kangxi_radical():
    """兼容部首（KANGXI RADICAL）折叠为标准 CJK 字符。"""
    # ⼯ = U+2F2F KANGXI RADICAL WORK，应归一化为「工」
    assert normalize_text("⼯作站OTA升级") == "工作站OTA升级"


def test_normalize_text_fullwidth():
    """全角字符折叠为半角。"""
    assert normalize_text("ＴＩＴＡＮ ８１０") == "TITAN 810"


def test_normalize_text_preserves_empty():
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def test_extract_entities_software_and_system():
    """OTA 文档中的工作站/固件/MobaXterm 应被识别。"""
    text = "使用 MobaXterm 登录工作站，通过 OTA 升级上传固件到服务器。"
    entities = _extract_entities_from_text(text)
    by_type: dict[str, set[str]] = {}
    for etype, name in entities:
        by_type.setdefault(etype, set()).add(name)
    assert "MobaXterm" in by_type.get("software", set())
    assert "工作站" in by_type.get("system", set())
    assert "服务器" in by_type.get("system", set())
    assert "固件" in by_type.get("artifact", set())
    assert "OTA 升级" in by_type.get("operation", set())


def test_extract_entities_normalized_input():
    """含兼容部首的文本归一化后仍能命中规则。"""
    text = "⼯作站OTA升级"  # ⼯为 KANGXI RADICAL
    entities = _extract_entities_from_text(text)
    names = {name for _etype, name in entities}
    assert "工作站" in names


def test_extract_entities_component_and_tool():
    text = "更换电池模块，用万用表测量电压 48。"
    entities = _extract_entities_from_text(text)
    by_type: dict[str, set[str]] = {}
    for etype, name in entities:
        by_type.setdefault(etype, set()).add(name)
    assert "电池模块" in by_type.get("component", set())
    assert "万用表" in by_type.get("tool", set())
    # parameter 规则：「电压 48」→ 电压
    assert any("电压" in name for name in by_type.get("parameter", set()))
