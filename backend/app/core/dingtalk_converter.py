import csv
import io
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from app.config import settings
from app.core.dingtalk import DingTalkClient
from app.core.dingtalk_storage import DingTalkLocalStorage
from app.core.markitdown_adapter import (
    MarkdownConversionResult,
    MarkItDownAdapter,
)
from app.core.pdf_hybrid_converter import PDFHybridConverter


TEXT_EXTENSIONS = {"md", "txt"}
CONVERSION_PIPELINE_VERSION = "dingtalk-markdown-pipeline-v24"


class DingTalkMarkdownConverter:
    """将本地钉钉原文件转换并持久化为统一Markdown。"""

    def __init__(
        self,
        storage: DingTalkLocalStorage | None = None,
        markitdown: MarkItDownAdapter | None = None,
    ):
        self.storage = storage or DingTalkLocalStorage()
        self.markitdown = markitdown or MarkItDownAdapter()
        self.pdf_hybrid = PDFHybridConverter(self.markitdown)

    @staticmethod
    def document_from_manifest(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": entry.get("document_id") or "",
            "title": entry.get("name") or "未命名",
            "extension": entry.get("extension") or "",
            "node_type": entry.get("node_type") or "",
            "space_id": entry.get("space_id") or "",
            "space_name": entry.get("space_name") or "",
            "path": entry.get("dingtalk_path") or "",
            "source_url": entry.get("source_url") or "",
            "updated_at": entry.get("source_updated_at") or "",
            "file_size": entry.get("reported_file_size"),
        }

    @staticmethod
    def _decode_text(content: bytes) -> str:
        encodings = ["utf-8-sig", "gb18030"]
        if content.startswith((b"\xff\xfe", b"\xfe\xff")):
            encodings.insert(1, "utf-16")
        for encoding in encodings:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return ""

    @staticmethod
    def _invalid_control_ratio(text: str) -> float:
        if not text:
            return 0.0
        invalid = sum(
            unicodedata.category(character) == "Cc"
            and character not in "\n\r\t\f"
            for character in text
        )
        return invalid / len(text)

    @classmethod
    def _validated_text(cls, text: str) -> str:
        """拒绝二进制误解码结果，并清理文本中无意义的分页控制符。"""
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip() or "\x00" in normalized:
            return ""
        if cls._invalid_control_ratio(normalized) > 0.005:
            return ""
        normalized = normalized.replace("\f", "\n")
        normalized = "".join(
            character
            for character in normalized
            if unicodedata.category(character) != "Cc" or character in "\n\t"
        )
        # 部分PDF字体缺少完整Unicode映射，正文仍可用但会出现少量替换字符。
        replacement_ratio = normalized.count("\ufffd") / max(len(normalized), 1)
        return normalized.strip() if replacement_ratio <= 0.1 else ""

    @staticmethod
    def _split_markdown_table_row(line: str) -> List[str]:
        source = line.strip().strip("|")
        cells: List[str] = []
        current: List[str] = []
        escaped = False
        for character in source:
            if escaped:
                current.append(character)
                escaped = False
            elif character == "\\":
                current.append(character)
                escaped = True
            elif character == "|":
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        cells.append("".join(current).strip())
        return cells

    @classmethod
    def _normalize_markdown_tables(cls, text: str) -> str:
        """为 MarkItDown 因合并单元格省略的尾部空单元格补位。"""
        lines = str(text or "").splitlines()
        is_row = lambda value: (
            value.lstrip().startswith("|") and value.rstrip().endswith("|")
        )
        is_separator = lambda value: (
            "-" in value and bool(re.fullmatch(r"[\s|:\-]+", value))
        )
        for separator_index, separator in enumerate(lines):
            if (
                not is_row(separator)
                or not is_separator(separator)
                or separator_index == 0
                or not is_row(lines[separator_index - 1])
            ):
                continue
            expected_width = len(cls._split_markdown_table_row(separator))
            row_index = separator_index - 1
            while row_index < len(lines) and is_row(lines[row_index]):
                if (
                    row_index > separator_index
                    and row_index + 1 < len(lines)
                    and is_row(lines[row_index + 1])
                    and is_separator(lines[row_index + 1])
                ):
                    break
                cells = cls._split_markdown_table_row(lines[row_index])
                if len(cells) < expected_width:
                    cells.extend([""] * (expected_width - len(cells)))
                    lines[row_index] = "| " + " | ".join(cells) + " |"
                row_index += 1
        return "\n".join(lines)

    @classmethod
    def _strict_plain_text(cls, content: bytes) -> str:
        """仅在原始字节确实像文本时启用格式标注错误的兼容兜底。"""
        if not content:
            return ""
        invalid_bytes = sum(
            byte < 32 and byte not in (9, 10, 12, 13)
            for byte in content
        )
        if invalid_bytes / len(content) > 0.005:
            return ""
        return cls._validated_text(cls._decode_text(content))

    @staticmethod
    def _encrypted_source_type(content: bytes) -> str:
        """识别企业终端安全软件生成的加密文件容器。"""
        header = bytes(content[:512])
        if b"E-SafeNet" in header and b"LOCK" in header:
            return "E-SafeNet"
        return ""

    @staticmethod
    def _markdown_cell(value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")

    @classmethod
    def _csv_to_markdown(cls, content: bytes) -> str:
        text = cls._decode_text(content)
        rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        lines = [
            "| " + " | ".join(cls._markdown_cell(cell) for cell in normalized[0]) + " |",
            "| " + " | ".join("---" for _ in range(width)) + " |",
        ]
        lines.extend(
            "| " + " | ".join(cls._markdown_cell(cell) for cell in row) + " |"
            for row in normalized[1:]
        )
        return "\n".join(lines)

    @staticmethod
    @contextmanager
    def _pdf_conversion_mode():
        previous = (
            settings.pdf_ocr_enabled,
            settings.pdf_image_assets_enabled,
            settings.pdf_vision_enabled,
        )
        if not settings.dingtalk_markdown_pdf_enhanced:
            settings.pdf_ocr_enabled = False
            settings.pdf_image_assets_enabled = False
            settings.pdf_vision_enabled = False
        try:
            yield
        finally:
            (
                settings.pdf_ocr_enabled,
                settings.pdf_image_assets_enabled,
                settings.pdf_vision_enabled,
            ) = previous

    def convert_content_result(
        self,
        content: bytes,
        extension: str,
    ) -> MarkdownConversionResult:
        extension = extension.lower().lstrip(".")
        encrypted_source = self._encrypted_source_type(content)
        if encrypted_source:
            return MarkdownConversionResult(
                "",
                "encrypted_source",
                warnings=[
                    f"检测到{encrypted_source}加密文件容器；"
                    "MarkItDown、OCR和Office解析器无法直接读取，"
                    "需要由有权限的安全终端导出解密原文件后再同步"
                ],
                metadata={"source_encryption": encrypted_source},
            )
        if extension in TEXT_EXTENSIONS:
            return MarkdownConversionResult(
                self._validated_text(self._decode_text(content)),
                "builtin_text",
            )
        if extension == "csv":
            return MarkdownConversionResult(
                self._validated_text(self._csv_to_markdown(content)),
                "builtin_csv",
            )
        if extension == "pdf":
            if not content.startswith(b"%PDF"):
                return MarkdownConversionResult(
                    self._strict_plain_text(content),
                    "plain_text_fallback",
                    fallback_used=True,
                )
            with self._pdf_conversion_mode():
                conversion = self.pdf_hybrid.convert(content)
            return MarkdownConversionResult(
                self._validated_text(conversion.text),
                conversion.converter,
                fallback_used=conversion.fallback_used,
                warnings=list(conversion.warnings),
                metadata=dict(conversion.metadata),
            )

        warnings: List[str] = []
        markitdown_attempted = bool(
            settings.markitdown_enabled
            and extension in self.markitdown.SUPPORTED_EXTENSIONS
        )
        if markitdown_attempted:
            try:
                converted = self._validated_text(
                    self._normalize_markdown_tables(
                        self.markitdown.convert_bytes(content, extension)
                    )
                )
                if converted:
                    return MarkdownConversionResult(converted, "markitdown")
                warnings.append("MarkItDown返回了空正文")
            except Exception as exc:
                warnings.append(str(exc))

        converted = self._validated_text(
            DingTalkClient._extract_text(content, extension)
        )
        if converted:
            return MarkdownConversionResult(
                converted,
                f"legacy_{extension or 'unknown'}",
                fallback_used=markitdown_attempted,
                warnings=warnings,
            )
        return MarkdownConversionResult(
            self._strict_plain_text(content),
            "plain_text_fallback",
            fallback_used=True,
            warnings=warnings,
        )

    @classmethod
    def convert_content(cls, content: bytes, extension: str) -> str:
        """兼容旧调用方；新流水线使用convert_content_result记录转换器信息。"""
        return cls().convert_content_result(content, extension).text

    @staticmethod
    def _frontmatter_value(value: Any) -> str:
        return json.dumps(str(value or ""), ensure_ascii=False)

    @classmethod
    def build_markdown(
        cls,
        entry: Dict[str, Any],
        body: str,
        conversion: MarkdownConversionResult | None = None,
    ) -> str:
        title = str(entry.get("name") or "未命名").replace("\r", " ").replace("\n", " ").strip()
        metadata = {
            "title": title,
            "source_type": "dingtalk",
            "dingtalk_node_id": entry.get("document_id") or "",
            "dingtalk_space_id": entry.get("space_id") or "",
            "dingtalk_space_name": entry.get("space_name") or "",
            "dingtalk_path": entry.get("dingtalk_path") or "",
            "source_url": entry.get("source_url") or "",
            "source_file": entry.get("raw_path") or "",
            "source_hash": entry.get("source_file_hash") or "",
            "source_updated_at": entry.get("source_updated_at") or "",
            "converter": conversion.converter if conversion else "",
            "converter_fallback_used": (
                "true" if conversion and conversion.fallback_used else "false"
            ),
            "conversion_pipeline_version": CONVERSION_PIPELINE_VERSION,
        }
        if conversion:
            for key, value in conversion.metadata.items():
                if key.startswith("pdf_"):
                    metadata[key] = value
        frontmatter = ["---"]
        frontmatter.extend(
            f"{key}: {cls._frontmatter_value(value)}"
            for key, value in metadata.items()
        )
        frontmatter.append("---")
        return "\n".join(frontmatter) + f"\n\n# {title}\n\n{body.strip()}\n"

    def _raw_path(self, entry: Dict[str, Any]) -> Path:
        relative = str(entry.get("raw_path") or "").strip()
        if not relative:
            raise ValueError("同步清单缺少raw_path")
        path = (self.storage.root / relative).resolve()
        self.storage._assert_contained(path, self.storage.raw_root)
        if not path.is_file():
            raise FileNotFoundError(f"本地原文件不存在: {path}")
        return path

    def convert_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        document = self.document_from_manifest(entry)
        conversion: MarkdownConversionResult | None = None
        try:
            raw_path = self._raw_path(entry)
            extension = raw_path.suffix.lower().lstrip(".")
            conversion = self.convert_content_result(raw_path.read_bytes(), extension)
            if not conversion.text:
                detail = "；".join(
                    warning for warning in conversion.warnings if warning
                )
                message = f"{extension or '未知格式'}文件未提取到有效正文"
                raise ValueError(f"{message}：{detail}" if detail else message)
            markdown = self.build_markdown(entry, conversion.text, conversion)
            conversion_metadata = {
                "converter": conversion.converter,
                "converter_fallback_used": conversion.fallback_used,
                "conversion_warnings": list(conversion.warnings),
                "conversion_pipeline_version": CONVERSION_PIPELINE_VERSION,
            }
            conversion_metadata.update(conversion.metadata)
            metadata = self.storage.persist_markdown_file(
                document,
                markdown,
                conversion_metadata=conversion_metadata,
            )
            result = dict(entry)
            result.update(metadata)
            result.update(conversion_metadata)
            result["status"] = "converted"
            result["error"] = None
            return result
        except Exception as exc:
            failure_metadata: Dict[str, Any] = {}
            if conversion is not None:
                failure_metadata = {
                    "converter": conversion.converter,
                    "converter_fallback_used": conversion.fallback_used,
                    "conversion_warnings": list(conversion.warnings),
                    **dict(conversion.metadata),
                }
            self.storage.update_document_status(
                document,
                "conversion_failed",
                error=str(exc),
                raw_path=entry.get("raw_path") or "",
                **failure_metadata,
            )
            raise

    def convert_manifest(
        self,
        force: bool = False,
        retry_failed: bool = True,
        extensions: Optional[Iterable[str]] = None,
        document_ids: Optional[Iterable[str]] = None,
        on_progress: Optional[Callable[[Dict[str, Any], int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        manifest = self.storage.read_manifest()
        eligible_statuses = {"downloaded"}
        if retry_failed:
            eligible_statuses.add("conversion_failed")
        allowed_extensions = {
            str(extension).lower().lstrip(".") for extension in (extensions or [])
            if str(extension).strip()
        }
        selected_ids = {
            str(document_id).strip() for document_id in (document_ids or [])
            if str(document_id).strip()
        }
        entries = [
            entry for entry in manifest["documents"]
            if (force or entry.get("status") in eligible_statuses)
            and (
                not selected_ids
                or str(entry.get("document_id") or "") in selected_ids
            )
            and (
                not allowed_extensions
                or Path(str(entry.get("raw_path") or "")).suffix.lower().lstrip(".")
                in allowed_extensions
            )
        ]
        converted = 0
        failed = 0
        failures: List[Dict[str, str]] = []
        for index, entry in enumerate(entries, 1):
            status = "converted"
            try:
                self.convert_entry(entry)
                converted += 1
            except Exception as exc:
                status = "conversion_failed"
                failed += 1
                failures.append({
                    "document_id": str(entry.get("document_id") or ""),
                    "name": str(entry.get("name") or "未命名"),
                    "error": str(exc),
                })
            if on_progress:
                on_progress(entry, index, len(entries), status)
        return {
            "total": len(entries),
            "converted": converted,
            "failed": failed,
            "failures": failures,
        }

    def quarantine_invalid_markdown(self) -> Dict[str, Any]:
        """隔离包含大量控制字符的历史Markdown，并恢复为待转换状态。"""
        manifest = self.storage.read_manifest()
        rejected_root = (self.storage.root / "rejected-markdown").resolve()
        rejected = []
        for entry in manifest["documents"]:
            if entry.get("status") != "converted":
                continue
            relative = str(entry.get("markdown_path") or "").strip()
            if not relative:
                continue
            source = (self.storage.root / relative).resolve()
            self.storage._assert_contained(source, self.storage.markdown_root)
            if not source.is_file():
                continue
            try:
                markdown = source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                markdown = "\x00"
            if self._validated_text(markdown):
                continue

            relative_markdown = source.relative_to(self.storage.markdown_root)
            destination = (rejected_root / relative_markdown).resolve()
            self.storage._assert_contained(destination, rejected_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
                destination = destination.with_name(
                    f"{destination.stem}.{timestamp}{destination.suffix}"
                )
            os.replace(source, destination)

            document = self.document_from_manifest(entry)
            rejected_path = destination.relative_to(self.storage.root).as_posix()
            self.storage.update_document_status(
                document,
                "downloaded",
                error="历史Markdown包含二进制控制字符，已隔离并等待重新转换",
                force_reconvert=True,
                raw_path=entry.get("raw_path") or "",
                markdown_hash=None,
                markdown_size=None,
                markdown_write_result=None,
                rejected_markdown_path=rejected_path,
                rejected_at=datetime.now(timezone.utc).isoformat(),
            )
            rejected.append({
                "document_id": str(entry.get("document_id") or ""),
                "name": str(entry.get("name") or "未命名"),
                "path": rejected_path,
            })
        return {"rejected": len(rejected), "documents": rejected}
