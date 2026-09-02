"""文本相似度公共工具（V4 Phase D-2 封板）。

供 raw_retriever.merge_rounds 与 raw_sufficiency_judge 共用同一套
中文友好相似度实现，避免两处各自实现造成不一致，也避免
raw_retriever ↔ raw_sufficiency_judge 循环 import。

中文相似度不采用 content.split()（中文无空格分词），改用：
- NFKC 归一化 + strip + 连续空白归一化；
- 相邻字符 bigram 集合的 Jaccard 相似度。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata


def normalize_content(text: str) -> str:
    """NFKC 归一化 + strip + 连续空白归一化。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = t.strip()
    return re.sub(r"\s+", " ", t)


def content_hash(text: str) -> str:
    """归一化后内容的 sha256 十六进制。"""
    return hashlib.sha256(normalize_content(text).encode("utf-8")).hexdigest()


def char_bigrams(text: str) -> set[str]:
    """归一化文本的相邻字符 bigram 集合（对中英文局部差异敏感）。"""
    norm = normalize_content(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def content_similarity(a: str, b: str) -> float:
    """字符 bigram Jaccard 相似度（0.0~1.0）。"""
    ta = char_bigrams(a)
    tb = char_bigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
