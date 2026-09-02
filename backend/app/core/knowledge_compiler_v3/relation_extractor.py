"""逐 Claim 关系提取（P5 修复：不再对 Card 内实体做笛卡尔组合）。

必须在同一条 Claim 内发现两个明确实体和关系词后才建立关系。
支持的关系词映射见 RELATION_KEYWORDS。

关系词两侧实体按「文本出现位置」就近匹配：左侧选 end 最接近关系词起点的
实体，右侧选 start 最接近关系词终点的实体；距离相同优先更长的实体名。
文本与实体名都经 NFKC 归一化，保证与实体抽取使用同一套结果。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.text_normalization import normalize_text

# 关系词 → 关系类型。
# 仅收录两侧都需要明确实体的强关系词；「通过/进行/进入/有/是」等宽泛词一律不收。
RELATION_KEYWORDS = {
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

# 关系词按长度降序，优先匹配更具体的关系词（如"安装在"优先于"在"）
_SORTED_KEYWORDS = sorted(RELATION_KEYWORDS, key=len, reverse=True)

# 关系词必须两侧都有明确实体才成立（避免把"属于"当通用词）
_RELATION_PATTERN = re.compile(
    r"(.{1,40}?)"
    r"(?:"
    + "|".join(re.escape(kw) for kw in _SORTED_KEYWORDS)
    + r")"
    r"(.{1,40}?)$"
)


@dataclass
class ExtractedRelation:
    source_entity: str          # 源实体名（原文）
    target_entity: str          # 目标实体名（原文）
    relation_type: str          # belongs_to/contains/...
    source_type: str = ""       # 实体类型（component/parameter/product/...）
    target_type: str = ""       # 实体类型
    confidence: float = 0.8


def extract_relations_from_claim(
    claim_text: str,
    entities: list[tuple[str, str]],
) -> list[ExtractedRelation]:
    """从单条 Claim 提取关系。

    流程：
    1. 文本与实体名都做 NFKC 归一化，与实体抽取同一套结果；
    2. 找出每个实体名在文本中的全部出现位置（finditer，忽略大小写）；
    3. 对每个关系词出现位置，左侧选 end <= 关系词起点且 end 最近（相同取更长名），
       右侧选 start >= 关系词终点且 start 最近（相同取更长名）；
    4. 按 (源实体, 关系类型, 目标实体) 去重。

    Args:
        claim_text: Claim 的 statement 文本。
        entities: 从该 Claim 提取出的实体列表 [(entity_type, name)]。

    Returns:
        只包含在同一条 Claim 内同时出现两个实体和一个关系词的关系。
    """
    if not claim_text or len(entities) < 2:
        return []

    text = normalize_text(claim_text).strip()
    if not text:
        return []

    # 每个实体的全部出现位置：[(start, end, entity_type, norm_name)]
    occurrences: list[tuple[int, int, str, str]] = []
    for etype, name in entities:
        norm_name = normalize_text(name)
        if not norm_name:
            continue
        for m in re.finditer(re.escape(norm_name), text, re.IGNORECASE):
            occurrences.append((m.start(), m.end(), etype, norm_name))

    found: list[ExtractedRelation] = []
    seen: set[tuple[str, str, str]] = set()

    for kw in _SORTED_KEYWORDS:
        for km in re.finditer(re.escape(kw), text):
            kw_start = km.start()
            kw_end = km.end()

            # 左侧：结束位置 <= 关系词起点，end 最大最接近；相同取更长实体名
            left = [occ for occ in occurrences if occ[1] <= kw_start]
            # 右侧：开始位置 >= 关系词终点，start 最小最接近；相同取更长实体名
            right = [occ for occ in occurrences if occ[0] >= kw_end]
            if not left or not right:
                continue

            left_occ = max(left, key=lambda o: (o[1], len(o[3])))
            right_occ = min(right, key=lambda o: (o[0], -len(o[3])))

            relation_type = RELATION_KEYWORDS[kw]
            key = (left_occ[3], relation_type, right_occ[3])
            if key in seen:
                continue
            seen.add(key)
            found.append(ExtractedRelation(
                source_entity=left_occ[3],
                target_entity=right_occ[3],
                relation_type=relation_type,
                source_type=left_occ[2],
                target_type=right_occ[2],
            ))

    return found
