"""产品版本识别与语义版本排序（V4 Phase I：Version-aware Wiki）。

本模块是纯函数、自包含、确定性的版本事实层，不 import 数据库模型 / settings /
jieba / LLM。所有版本识别必须落到这里的确定性规则，LLM 只能辅助提取候选，不能
凭空猜版本，也不作为版本的唯一权限来源。

核心语义（V4 Phase I 二）：
- 产品版本：2.0、3.0、v3.1.2 等知识适用版本，用 version_label / version_sort_key
  表达；latest_version 必须有确定依据（可比较语义版本中的最大值）。
- SourceItem.external_version（同步版本/提交号/更新时间）不是产品版本，本模块
  不把 external_version 当作产品版本。
- 一个来源同时声称多个互相冲突的产品版本 → ambiguous/unversioned，绝不任意选择。
- 无法可靠比较的标签：稳定排序，但绝不擅自声明为最新版。

版本识别优先级（V4 Phase I 二，由调用方传入候选并按序尝试）：
1. 明确结构化元数据（metadata 里显式 version 字段）；
2. 文件标题 / 文件名 / 目录路径中的明确版本；
3. 正文中的明确版本声明；
4. 无可靠证据 → unversioned（版本未标明）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 版本常量（V4 Phase I 三）
VERSION_STATUS_CONFIRMED = "confirmed"      # 有明确且唯一的结构化版本证据
VERSION_STATUS_UNVERSIONED = "unversioned"  # 无可靠版本证据（版本未标明）
VERSION_STATUS_AMBIGUOUS = "ambiguous"      # 多个互相冲突的版本声明，无法唯一确定

CONTENT_ORIGIN_AUTO = "auto"
CONTENT_ORIGIN_MANUAL = "manual"

MERGE_POLICY_AUTO = "auto"           # 自动内容，可被刷新
MERGE_POLICY_PROTECTED = "protected"  # 人工保护，自动刷新不得覆盖

IS_COMMON_LABEL = "common"
UNVERSIONED_LABEL = "unversioned"


# ---------------------------------------------------------------------------
# 版本正则与候选提取
# ---------------------------------------------------------------------------

# 语义版本：捕获 v 前缀、主/次/补丁（次与补丁可选）。
# - 带 v/V 前缀时允许单段（v3）；
# - 不带前缀时至少需要 x.y 两段（避免把年份/ID 当版本）。
_SEMVER_RE = re.compile(
    r"(?<![\w.])(?P<full>v(?P<vmajor>\d{1,4})(?:\.(?P<vminor>\d{1,4}))?(?:\.(?P<vpatch>\d{1,4}))?|"
    r"(?P<major>\d{1,4})\.(?P<minor>\d{1,4})(?:\.(?P<patch>\d{1,4}))?)(?![\w.])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SemVer:
    """解析后的语义版本。major/minor/patch 缺省为 0。"""

    label: str          # 规范化标签（去掉 v/V 前缀、补齐缺失段，如 "3.0" / "2.0.1"）
    raw: str            # 原文
    major: int
    minor: int
    patch: int


def parse_semver(text: str) -> SemVer | None:
    """从一段文本解析一个语义版本；无匹配返回 None。"""
    if not text:
        return None
    m = _SEMVER_RE.search(text)
    if m is None:
        return None
    raw = m.group("full")
    if m.group("vmajor") is not None:
        major = int(m.group("vmajor"))
        minor = int(m.group("vminor") or 0)
        patch = int(m.group("vpatch") or 0)
    else:
        major = int(m.group("major"))
        minor = int(m.group("minor"))
        patch = int(m.group("patch") or 0)
    # 结构化生成规范标签，绝不使用 rstrip(".0")（否则 2.10→2.1、10.0→1.0）。
    # patch 非 0 才带 patch 段，否则保留 major.minor；单段 vN 补成 N.0。
    if patch != 0:
        label = f"{major}.{minor}.{patch}"
    else:
        label = f"{major}.{minor}"
    return SemVer(label=label, raw=raw, major=major, minor=minor, patch=patch)


def extract_version_labels(text: str) -> list[str]:
    """从一段文本提取所有语义版本标签（去重，保序）。

    只接受：带 v/V 前缀的单段或多段；不带前缀的至少 x.y 两段。
    """
    if not text:
        return []
    seen: list[str] = []
    for m in _SEMVER_RE.finditer(text or ""):
        sv = parse_semver(m.group(0))
        if sv is None:
            continue
        if sv.label not in seen:
            seen.append(sv.label)
    return seen


def normalize_version_label(label: str) -> str:
    """规范化版本标签：NFKC + 去空白 + 去 v/V 前缀 + 小写。

    返回用于比较/排序的规范标签；无法识别返回原始串（保留证据，但不可比较）。
    """
    if not label:
        return ""
    s = unicodedata.normalize("NFKC", str(label)).strip()
    if not s:
        return ""
    if s.lower() == UNVERSIONED_LABEL:
        return UNVERSIONED_LABEL
    if s.lower() == IS_COMMON_LABEL:
        return IS_COMMON_LABEL
    sv = parse_semver(s)
    if sv is not None:
        return sv.label
    return s


# ---------------------------------------------------------------------------
# 语义版本排序
# ---------------------------------------------------------------------------

def _semver_sort_parts(label: str) -> tuple[int, int, int] | None:
    """可比较语义版本 → (major, minor, patch)；不可比较 → None。"""
    norm = normalize_version_label(label)
    if norm in (IS_COMMON_LABEL, UNVERSIONED_LABEL, ""):
        return None
    # 直接按规范标签解析；parse_semver 会拒掉不可比标签。
    sv = parse_semver(norm)
    return (sv.major, sv.minor, sv.patch) if sv is not None else None


def version_sort_key(label: str) -> tuple[int, object, int, int, int, str]:
    """确定性排序键。

    返回 (comparable_rank, stable_tiebreak, major, minor, patch, norm_label)。
    - comparable_rank=0 表示可比语义版本；1 表示不可比标签（排在其后）。
    - 排序用负数 major 降序（Python 默认升序，故这里返回负值或由调用方 reverse）。
    为保持纯函数稳定，本键按「数值升序」编码，调用方用 reverse=True 得到
    major 降序。不可比标签的 stable_tiebreak 用原始标签哈希保证稳定顺序。
    - norm_label 用规范化标签（2.10.0 与 2.10 归一化后同为 "2.10"），保证
      等价版本排序键完全一致。
    """
    norm = normalize_version_label(label)
    parts = _semver_sort_parts(label)
    if parts is None:
        return (1, "", 0, 0, 0, label or "")
    return (0, "", parts[0], parts[1], parts[2], norm)


def latest_version(labels: list[str]) -> str | None:
    """从一组版本标签确定性计算最新版本。

    - 只考虑「可比较语义版本」；无可比较版本返回 None（不擅自声明最新）。
    - 语义版本按 (major, minor, patch) 降序取最大；同值按标签稳定去重后选最大。
    """
    comparable: list[tuple[int, int, int, str]] = []
    for label in labels:
        parts = _semver_sort_parts(label)
        if parts is not None:
            comparable.append((parts[0], parts[1], parts[2], normalize_version_label(label)))
    if not comparable:
        return None
    comparable.sort(key=lambda t: (t[0], t[1], t[2], t[3]), reverse=True)
    return comparable[0][3]


# ---------------------------------------------------------------------------
# 产品版本识别
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VersionDetection:
    """版本识别结果（确定性，不依赖 LLM）。"""

    version_label: str          # 产品版本标签（"2.0"/"3.0"/"unversioned"/"ambiguous"）
    version_status: str         # confirmed / unversioned / ambiguous
    version_sort_key: tuple[int, object, int, int, int, str]
    candidates: tuple[str, ...] = ()  # 提取到的全部候选（供审计）

    @property
    def is_versioned(self) -> bool:
        return self.version_status == VERSION_STATUS_CONFIRMED


def detect_product_version(
    *,
    structured_version: str | None = None,
    title: str | None = None,
    source_path: str | None = None,
    content: str | None = None,
) -> VersionDetection:
    """按优先级识别产品版本。

    优先级（V4 Phase I 二）：
    1. structured_version（明确结构化元数据，最可信）；
    2. title（标题 / 文件名）；
    3. source_path（目录路径 / 文件名）；
    4. content（正文明确版本声明）。
    5. 全部无可靠证据 → unversioned。

    每一层若出现多个互不相同的版本 → ambiguous，不继续下沉（上层冲突即停）。
    """
    for candidates in _ordered_candidates(structured_version, title, source_path, content):
        labels = candidates
        if not labels:
            continue
        # 单个来源内多个互相冲突的版本 → ambiguous，绝不任意选。
        unique = list(dict.fromkeys(labels))
        if len(unique) > 1:
            return _ambiguous(unique)
        label = unique[0]
        key = version_sort_key(label)
        return VersionDetection(
            version_label=normalize_version_label(label),
            version_status=VERSION_STATUS_CONFIRMED,
            version_sort_key=key,
            candidates=tuple(unique),
        )

    key = version_sort_key(UNVERSIONED_LABEL)
    return VersionDetection(
        version_label=UNVERSIONED_LABEL,
        version_status=VERSION_STATUS_UNVERSIONED,
        version_sort_key=key,
        candidates=(),
    )


def _ordered_candidates(
    structured_version: str | None,
    title: str | None,
    source_path: str | None,
    content: str | None,
) -> list[list[str]]:
    """按优先级生成候选层；每层为一个列表（可能空）。"""
    layers: list[list[str]] = []
    if structured_version:
        layers.append(extract_version_labels(structured_version))
    if title:
        layers.append(extract_version_labels(title))
    if source_path:
        layers.append(extract_version_labels(source_path))
    if content:
        layers.append(extract_version_labels(content))
    return layers


def _ambiguous(candidates: list[str]) -> VersionDetection:
    return VersionDetection(
        version_label="ambiguous",
        version_status=VERSION_STATUS_AMBIGUOUS,
        version_sort_key=(1, "", 0, 0, 0, "ambiguous"),
        candidates=tuple(candidates),
    )
