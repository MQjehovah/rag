"""P5 修复：逐 Claim 关系提取测试。

覆盖文档 5.8 验证方法：
- 「电池模块属于 SKYWALKER 50」→ 一条 belongs_to 关系
- 不把 Card 内所有 component 与 product 做笛卡尔组合
- 同一 Claim 无两个实体时不生成关系
"""
from __future__ import annotations

from app.core.knowledge_compiler_v3.entity_patterns import ENTITY_PATTERNS
from app.core.knowledge_compiler_v3.relation_extractor import (
    RELATION_KEYWORDS,
    extract_relations_from_claim,
)


def _extract_entities(text: str) -> list[tuple[str, str]]:
    import re
    entities: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entity_type, patterns in ENTITY_PATTERNS:
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                name = m.group(0).strip()
                key = (entity_type, name)
                if name and key not in seen:
                    seen.add(key)
                    entities.append(key)
    return entities


def test_belongs_to_from_single_claim():
    """文档 5.8 核心：'电池模块属于 SKYWALKER 50' → 一条 belongs_to。"""
    text = "电池模块属于 SKYWALKER 50"
    entities = _extract_entities(text)
    # 应抽到 component(电池模块) + product(SKYWALKER 50)
    assert ("component", "电池模块") in entities
    assert ("product", "SKYWALKER 50") in entities

    rels = extract_relations_from_claim(text, entities)
    assert len(rels) == 1
    rel = rels[0]
    assert rel.relation_type == "belongs_to"
    assert rel.source_entity == "电池模块"
    assert rel.target_entity == "SKYWALKER 50"


def test_no_cartesian_explosion():
    """Card 有多个 component 和 product 时，不跨 Claim 做笛卡尔组合。"""
    # 模拟：一个卡里有两个实体但出现在不同 Claim，逐 Claim 提取不应交叉
    entities = [("component", "电池模块"), ("component", "驱动电机"), ("product", "SKYWALKER 50")]

    # 单条 Claim 只提一个实体对
    rels1 = extract_relations_from_claim("电池模块属于 SKYWALKER 50", entities)
    assert len(rels1) == 1

    # 无关系词的 Claim 不生成
    rels2 = extract_relations_from_claim("驱动电机正常工作", entities)
    assert len(rels2) == 0


def test_requires_two_entities_in_same_claim():
    """同一条 Claim 只有一个实体时不生成关系。"""
    entities = [("product", "SKYWALKER 50")]
    rels = extract_relations_from_claim("SKYWALKER 50 属于某系列", entities)
    assert len(rels) == 0


def test_multiple_relation_keywords():
    """不同关系词映射正确。"""
    entities = [("component", "充电桩"), ("product", "SKYWALKER 50")]
    rels = extract_relations_from_claim("充电桩连接到 SKYWALKER 50", entities)
    assert len(rels) == 1
    assert rels[0].relation_type == "connects_to"


def test_relation_keyword_map_complete():
    """文档 5.3 要求的关系词映射齐全（含 OTA 场景扩充）。"""
    expected = {
        "属于": "belongs_to",
        "包含": "contains",
        "安装在": "installed_on",
        "连接到": "connects_to",
        "依赖": "requires",
        "适用于": "applies_to",
        "支持": "supports",
        "导致": "leads_to",
        "解决": "solves",
        "替代": "supersedes",
        "上传到": "uploaded_to",
        "上传至": "uploaded_to",
        "运行于": "runs_on",
        "运行在": "runs_on",
        "部署到": "deployed_to",
        "部署在": "deployed_to",
        "调用": "invokes",
        "使用": "uses",
        "保存到": "stored_in",
        "保存至": "stored_in",
        "烧录到": "flashed_to",
        "烧录进": "flashed_to",
        "刷写到": "flashed_to",
    }
    assert RELATION_KEYWORDS == expected


def test_uploaded_to_prefers_nearest_left_entity():
    """「将工作站固件上传到公司服务器」→ 固件 --uploaded_to--> 服务器。

    左侧有工作站、固件两个候选，必须选结束位置最接近关系词的固件。
    """
    entities = [("system", "工作站"), ("artifact", "固件"), ("system", "服务器")]
    rels = extract_relations_from_claim("将工作站固件上传到公司服务器", entities)
    assert len(rels) == 1
    rel = rels[0]
    assert rel.relation_type == "uploaded_to"
    assert rel.source_entity == "固件"
    assert rel.target_entity == "服务器"


def test_normalizes_kangxi_radical_in_relation():
    """「⼯作站连接到服务器」NFKC 归一化后得到 工作站 --connects_to--> 服务器。"""
    entities = [("system", "⼯作站"), ("system", "服务器")]
    rels = extract_relations_from_claim("⼯作站连接到服务器", entities)
    assert len(rels) == 1
    rel = rels[0]
    assert rel.relation_type == "connects_to"
    assert rel.source_entity == "工作站"
    assert rel.target_entity == "服务器"


def test_same_keyword_multiple_occurrences():
    """同一关系词在一条 Claim 出现两次，生成两条关系。"""
    entities = [("artifact", "固件"), ("artifact", "日志"), ("system", "服务器")]
    rels = extract_relations_from_claim("固件上传到服务器，日志上传到服务器", entities)
    pairs = {(r.source_entity, r.relation_type, r.target_entity) for r in rels}
    assert ("固件", "uploaded_to", "服务器") in pairs
    assert ("日志", "uploaded_to", "服务器") in pairs
    assert len(pairs) == 2


def test_missing_side_yields_no_relation():
    """缺少任意一侧实体时不生成关系。"""
    # 缺右侧：无目标实体
    assert extract_relations_from_claim("固件上传到", [("artifact", "固件")]) == []
    # 缺左侧：无源实体
    assert extract_relations_from_claim("上传到服务器", [("system", "服务器")]) == []


def test_prefers_nearest_among_multiple_candidates():
    """多个候选实体时选距离关系词最近的实体（左右两侧都验证）。"""
    # 右侧两个候选：服务器(近)、工作站(远)，应选服务器
    entities = [("artifact", "固件"), ("system", "服务器"), ("system", "工作站")]
    rels = extract_relations_from_claim("固件上传到服务器工作站", entities)
    assert len(rels) == 1
    assert rels[0].source_entity == "固件"
    assert rels[0].target_entity == "服务器"
