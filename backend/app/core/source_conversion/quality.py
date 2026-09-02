"""Phase 1.1：文本质量诊断能力（从钉钉 v24 兼容移植）。

兼容移植声明：以下实现从 DingTalkMarkdownConverter 提取/复刻，行为与钉钉
v24 保持一致；DingTalkMarkdownConverter 尚未调用本模块，真正统一到共享实现
留到 Phase 2。本阶段通过参数化等价测试保证新旧行为一致。
"""
from __future__ import annotations

import unicodedata

from app.core.source_conversion.schemas import (
    ConversionDiagnostic,
    DiagnosticSeverity,
)


def decode_text(content: bytes) -> str:
    """UTF-8(-sig) → GB18030 → UTF-16 顺序解码；全部失败返回空串。

    与 DingTalkMarkdownConverter._decode_text 行为一致。
    """
    encodings = ["utf-8-sig", "gb18030"]
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.insert(1, "utf-16")
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def _is_binary_like(content: bytes) -> bool:
    """明显二进制判定：NUL 或控制字节占比高（排除 \n\r\t\f）。"""
    if not content:
        return True
    control = sum(
        byte < 32 and byte not in (9, 10, 12, 13)
        for byte in content
    )
    return control / len(content) > 0.005


def decode_text_strict(content: bytes) -> tuple[str, bool]:
    """严格解码：明显二进制 → ("", False)。

    规则（与名字和报告一致）：
    - 非 UTF-16 BOM 情况下先做 NUL/控制字节比例检查；
    - UTF-16 仅在合法 BOM（\xff\xfe / \xfe\xff）下尝试；
    - UTF-8/GB18030 解码后必须通过 validated_text 质量检查才返回 ok=True。
    """
    if not content:
        return "", False

    has_utf16_bom = content.startswith((b"\xff\xfe", b"\xfe\xff"))

    # 明显二进制预检（UTF-16 合法 BOM 的偶数长度内容除外）
    if not has_utf16_bom and _is_binary_like(content):
        return "", False

    def _try(encoding: str) -> str | None:
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            return None

    if has_utf16_bom:
        # UTF-16 需要偶数长度
        if len(content) % 2 != 0:
            return "", False
        text = _try("utf-16")
        if text is None:
            return "", False
        validated = validated_text(text)
        if not validated:
            return "", False
        return validated, True

    for enc in ["utf-8-sig", "gb18030"]:
        text = _try(enc)
        if text is None:
            continue
        validated = validated_text(text)
        if not validated:
            # 解码"成功"但质量不合格 → 视为非严格可接受，尝试下一个编码
            continue
        return validated, True
    return "", False


def invalid_control_ratio(text: str) -> float:
    if not text:
        return 0.0
    invalid = sum(
        unicodedata.category(char) == "Cc" and char not in "\n\r\t\f"
        for char in text
    )
    return invalid / len(text)


def validated_text(text: str) -> str:
    """拒绝二进制误解码结果，并清理无意义分页控制符。与钉钉一致。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip() or "\x00" in normalized:
        return ""
    if invalid_control_ratio(normalized) > 0.005:
        return ""
    normalized = normalized.replace("\f", "\n")
    normalized = "".join(
        ch for ch in normalized if unicodedata.category(ch) != "Cc" or ch in "\n\t"
    )
    replacement_ratio = normalized.count("�") / max(len(normalized), 1)
    return normalized.strip() if replacement_ratio <= 0.1 else ""


def strict_plain_text(content: bytes) -> str:
    """仅当原始字节确实像文本时返回解码结果（格式标注错误的兼容兜底）。"""
    if not content:
        return ""
    invalid = sum(byte < 32 and byte not in (9, 10, 12, 13) for byte in content)
    if invalid / len(content) > 0.005:
        return ""
    return validated_text(decode_text(content))


def encrypted_source_type(content: bytes) -> str:
    """识别企业终端安全软件生成的加密文件容器（如 E-SafeNet）。"""
    header = bytes(content[:512])
    if b"E-SafeNet" in header and b"LOCK" in header:
        return "E-SafeNet"
    return ""


def build_diagnostics(*, empty: bool = False, garbled: bool = False,
                      encrypted: str = "", fallback_used: bool = False,
                      warnings: list[str] | None = None,
                      decode_failed: bool = False) -> list[ConversionDiagnostic]:
    """把转换过程中的质量信号汇总为结构化 diagnostic 列表。"""
    diagnostics: list[ConversionDiagnostic] = []
    if encrypted:
        diagnostics.append(ConversionDiagnostic(
            code="encrypted_source", severity=DiagnosticSeverity.ERROR,
            message=f"检测到 {encrypted} 加密文件容器；需要由有权限的安全终端导出解密原文件后再同步",
            detail={"encryption": encrypted},
        ))
    if decode_failed:
        diagnostics.append(ConversionDiagnostic(
            code="decode_failed", severity=DiagnosticSeverity.ERROR,
            message="内容无法按 UTF-8/GB18030/UTF-16 解码",
        ))
    if garbled:
        diagnostics.append(ConversionDiagnostic(
            code="garbled_text", severity=DiagnosticSeverity.ERROR,
            message="文本包含二进制误解码/乱码，已拒绝转换",
        ))
    if empty:
        diagnostics.append(ConversionDiagnostic(
            code="empty_body", severity=DiagnosticSeverity.ERROR,
            message="未能从原文件提取到有效正文",
        ))
    if fallback_used:
        diagnostics.append(ConversionDiagnostic(
            code="fallback_used", severity=DiagnosticSeverity.WARNING,
            message="转换使用了降级路径",
        ))
    for warning in warnings or []:
        diagnostics.append(ConversionDiagnostic(
            code="converter_warning", severity=DiagnosticSeverity.WARNING,
            message=str(warning),
        ))
    return diagnostics
