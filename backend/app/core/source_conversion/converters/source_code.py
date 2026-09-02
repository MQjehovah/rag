"""源代码转换器：按语言映射包裹 fenced code block。"""
from __future__ import annotations

import re

from app.core.source_conversion.base import (
    ConverterMatch,
    EvidenceStrength,
    SourceConverter,
)
from app.core.source_conversion.converters.helpers import pick_strongest
from app.core.source_conversion.quality import decode_text_strict, validated_text
from app.core.source_conversion.schemas import (
    ContentKind,
    ConversionDiagnostic,
    ConversionStatus,
    ConverterOutput,
    DiagnosticSeverity,
    RawSourceItem,
    SourceBytesPayload,
    SourceTextPayload,
)

_EXT_LANG = {
    "py": "python", "pyw": "python", "pyi": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript", "tsx": "tsx", "jsx": "jsx",
    "java": "java", "kt": "kotlin", "kts": "kotlin",
    "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "hpp": "cpp", "cxx": "cpp",
    "go": "go", "rs": "rust", "rb": "ruby", "php": "php",
    "cs": "csharp", "swift": "swift", "scala": "scala", "dart": "dart",
    "sh": "bash", "bash": "bash", "zsh": "zsh", "ps1": "powershell", "bat": "bat", "cmd": "bat",
    "sql": "sql", "html": "html", "htm": "html", "xml": "xml", "css": "css",
    "scss": "scss", "less": "less", "json": "json", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "ini": "ini", "cfg": "ini", "conf": "ini",
    "gradle": "gradle", "tf": "terraform", "proto": "protobuf",
    "ipynb": "json",
}

_SPECIAL_FILENAMES = {
    "dockerfile": "dockerfile", "containerfile": "dockerfile",
    "makefile": "makefile", "gnumakefile": "makefile",
}

# MIME → 具体语言（精确 MIME 语言映射）
_MIME_LANG = {
    "application/javascript": "javascript",
    "text/javascript": "javascript",
    "application/json": "json",
    "text/x-python": "python",
    "text/x-python3": "python",
    "text/x-java-source": "java",
    "text/x-go": "go",
    "text/x-ruby": "ruby",
    "text/x-c": "c",
    "text/x-c++src": "cpp",
    "text/x-sh": "bash",
    "text/x-shellscript": "bash",
    "application/x-httpd-php": "php",
    "application/typescript": "typescript",
}

_SHEBANG = {
    "python": re.compile(r"^#!.*\bpython(?:3|3\.\d+)?\b"),
    "bash": re.compile(r"^#!.*\bbash\b"),
    "sh": re.compile(r"^#!.*\bsh\b"),
    "javascript": re.compile(r"^#!.*\bnode(?:js)?\b"),
}


class SourceCodeConverter:
    key = "source_code"
    version = "v1"
    priority = 5
    output_kind = ContentKind.CODE

    def match(self, item: RawSourceItem) -> ConverterMatch | None:
        candidates: list[ConverterMatch] = []
        # shebang 文件头（强证据，能确定具体语言）
        head = item.raw_bytes[:256].decode("utf-8", errors="replace")
        for lang, regex in _SHEBANG.items():
            if regex.match(head):
                candidates.append(ConverterMatch(
                    converter_key=self.key, specificity=EvidenceStrength.FILE_HEADER,
                    priority=self.priority, reason=f"shebang 文件头 → {lang}",
                    evidence={"shebang": head.splitlines()[0] if head.splitlines() else "", "lang": lang},
                ))
                break
        if item.content_kind == ContentKind.CODE:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.CONTENT_KIND,
                priority=self.priority, reason="明确 content_kind=code",
                evidence={"content_kind": "code"},
            ))
        mime = (item.mime_type or "").lower()
        if mime in _MIME_LANG:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.MIME,
                priority=self.priority, reason=f"代码 MIME {mime}",
                evidence={"mime": mime, "lang": _MIME_LANG[mime]},
            ))
        basename = (item.filename or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if basename in _SPECIAL_FILENAMES:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.EXTENSION,
                priority=self.priority, reason=f"特殊文件名 {basename}",
                evidence={"filename": basename, "lang": _SPECIAL_FILENAMES[basename]},
            ))
        if item.extension.lower() in _EXT_LANG:
            candidates.append(ConverterMatch(
                converter_key=self.key, specificity=EvidenceStrength.EXTENSION,
                priority=self.priority, reason=f"代码扩展名 .{item.extension.lower()}",
                evidence={"extension": item.extension.lower(), "lang": _EXT_LANG[item.extension.lower()]},
            ))
        merged = pick_strongest(candidates, converter_key=self.key, priority=self.priority)
        if merged is None:
            return None
        # 解析具体语言与证据：shebang > MIME > 特殊文件名/扩展名
        resolved_lang, lang_evidence = self._resolve_language(item, merged)
        merged_evidence = dict(merged.evidence or {})
        merged_evidence["resolved_language"] = resolved_lang
        merged_evidence["language_evidence"] = lang_evidence
        return ConverterMatch(
            converter_key=self.key,
            specificity=merged.specificity,
            priority=self.priority,
            reason=merged.reason,
            evidence=merged_evidence,
        )

    def _resolve_language(self, item: RawSourceItem, selected: ConverterMatch) -> tuple[str, str]:
        """解析具体语言与证据来源：shebang(FILE_HEADER) > MIME > 特殊文件名/扩展名 > 次级信号。

        content_kind=code 只证明是代码；具体语言由次级信号（shebang/MIME/扩展名/文件名）解析，
        不得把能确定的语言降为 text。
        """
        from collections.abc import Mapping
        evidence = selected.evidence or {}
        if isinstance(evidence, Mapping):
            # shebang 明确语言（FILE_HEADER 最强）
            if selected.specificity == EvidenceStrength.FILE_HEADER and evidence.get("lang"):
                return str(evidence["lang"]), "shebang"
            # MIME 语言映射
            lang = evidence.get("lang")
            if selected.specificity == EvidenceStrength.MIME and isinstance(lang, str) and lang:
                return lang, "mime"
            # 特殊文件名/扩展名
            if selected.specificity == EvidenceStrength.EXTENSION and isinstance(lang, str) and lang:
                return lang, "extension"
        # content_kind=code 无直接语言 → 解析次级信号（item 级 shebang/MIME/扩展名/特殊文件名）
        return self._secondary_language(item)

    def _secondary_language(self, item: RawSourceItem) -> tuple[str, str]:
        """从 item 解析次级语言信号：shebang > MIME > 特殊文件名/扩展名。"""
        head = item.raw_bytes[:256].decode("utf-8", errors="replace")
        for lang, regex in _SHEBANG.items():
            if regex.match(head):
                return lang, "shebang"
        mime = (item.mime_type or "").lower()
        if mime in _MIME_LANG:
            return _MIME_LANG[mime], "mime"
        basename = (item.filename or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if basename in _SPECIAL_FILENAMES:
            return _SPECIAL_FILENAMES[basename], "filename"
        ext = item.extension.lower()
        if ext in _EXT_LANG:
            return _EXT_LANG[ext], "extension"
        return "text", "default"

    def convert(self, item: RawSourceItem, selected_match: ConverterMatch) -> ConverterOutput:
        if isinstance(item.payload, SourceTextPayload):
            text = item.payload.text
        else:
            text, ok = decode_text_strict(item.payload.bytes)
            if not ok:
                return ConverterOutput(
                    body="", status_signal=ConversionStatus.FAILED,
                    diagnostics=(ConversionDiagnostic(
                        code="decode_failed", severity=DiagnosticSeverity.ERROR,
                        message="源代码无法解码",
                    ),),
                    content_kind=ContentKind.CODE,
                )
        code = validated_text(text)
        # 语言：优先 selected_match.evidence["resolved_language"]（与最强证据一致）
        from collections.abc import Mapping
        evidence = selected_match.evidence or {}
        lang = ""
        lang_evidence = ""
        if isinstance(evidence, Mapping):
            lang = evidence.get("resolved_language", "")
            lang_evidence = evidence.get("language_evidence", "")
        if not isinstance(lang, str) or not lang:
            lang, lang_evidence = self._detect_language_fallback(item)
        diagnostics: list[ConversionDiagnostic] = []
        if not (code or "").strip():
            diagnostics.append(ConversionDiagnostic(
                code="empty_body", severity=DiagnosticSeverity.ERROR,
                message="源代码内容为空或包含乱码，已拒绝转换",
            ))
        else:
            code = self._wrap_fence(code, lang)
        return ConverterOutput(
            body=code or "",
            status_signal=ConversionStatus.FAILED if not (code or "").strip() else ConversionStatus.CONVERTED,
            diagnostics=tuple(diagnostics),
            conversion_metadata={"code_lang": lang, "language_evidence": lang_evidence or "default"},
            content_kind=ContentKind.CODE,
        )

    def _detect_language_fallback(self, item: RawSourceItem) -> tuple[str, str]:
        """兜底语言检测（selected_match 无 resolved_language 时）。"""
        basename = (item.filename or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if basename in _SPECIAL_FILENAMES:
            return _SPECIAL_FILENAMES[basename], "filename"
        ext = item.extension.lower()
        if ext in _EXT_LANG:
            return _EXT_LANG[ext], "extension"
        head = item.raw_bytes[:256].decode("utf-8", errors="replace")
        for lang, regex in _SHEBANG.items():
            if regex.match(head):
                return lang, "shebang"
        mime = (item.mime_type or "").lower()
        if mime in _MIME_LANG:
            return _MIME_LANG[mime], "mime"
        return "text", "default"

    def _wrap_fence(self, code: str, lang: str) -> str:
        max_run = 0
        for m in re.finditer(r"`{3,}", code):
            max_run = max(max_run, m.end() - m.start())
        fence_len = max(3, max_run + 1)
        fence = "`" * fence_len
        return f"{fence}{lang}\n{code}\n{fence}"
