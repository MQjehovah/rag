"""文本归一化（NFKC）：把全角/兼容/部首字符折叠为标准形式。

解决 OCR 抽取中的兼容字符问题，例如：
- ⼯（U+2F2F KANGXI RADICAL WORK）→ 工（U+5DE5）
- ⽂（U+2F47 KANGXI RADICAL SCRIPT）→ 文（U+6587）
- 进⼊（⼊ U+2F00 系列）→ 进入

`unicodedata.normalize("NFKC", ...)` 会自动把这些兼容/部首字符折叠为
标准 CJK 字符，同时把全角英数字母折叠为半角。本模块是纯函数，不依赖
外部模型，是 GLM 不可用阶段唯一的归一化来源。
"""
from __future__ import annotations

import unicodedata


def normalize_text(value: str | None) -> str:
    """NFKC 归一化。空值原样返回，非空字符串折叠兼容字符。"""
    if not value:
        return value or ""
    return unicodedata.normalize("NFKC", value)
