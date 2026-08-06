"""页面正文写入前的通用质量检查。"""

from __future__ import annotations

from typing import Optional


_BINARY_PREFIXES = (
    ("%PDF-", "PDF原始二进制"),
    ("PK\x03\x04", "ZIP/Office原始二进制"),
    ("PK\x05\x06", "ZIP/Office原始二进制"),
    ("PK\x07\x08", "ZIP/Office原始二进制"),
    ("\x7fELF", "ELF原始二进制"),
)


def detect_binary_text(content: Optional[str]) -> str:
    """识别被错误解码成字符串的二进制文件，返回原因或空字符串。"""
    if not content:
        return ""

    sample = content.lstrip("\ufeff \t\r\n")[:8192]
    for prefix, reason in _BINARY_PREFIXES:
        if sample.startswith(prefix):
            return reason

    if "\x00" in sample:
        return "正文包含二进制空字节"

    control_count = sum(
        1
        for character in sample
        if ord(character) < 32 and character not in "\t\r\n"
    )
    if sample and control_count / len(sample) >= 0.01:
        return "正文包含过多二进制控制字符"

    # PDF按UTF-8忽略非法字节解码后，文件头偶尔会被前置字符干扰。
    first_block = sample[:2048]
    if "%PDF-" in first_block and " obj" in first_block and "stream" in sample:
        return "PDF原始二进制"

    return ""


def ensure_text_content(content: Optional[str]) -> None:
    """阻止原始PDF、Office压缩包等二进制内容进入页面正文。"""
    reason = detect_binary_text(content)
    if reason:
        raise ValueError(
            f"检测到{reason}，禁止直接写入笔记正文；请先转换为Markdown"
        )
