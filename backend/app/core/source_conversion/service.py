"""Phase 1.3：CanonicalNoteService（严格 fail closed + 保留字段 + output_kind 校验）。"""
from __future__ import annotations

import logging
import traceback

from app.core.source_conversion.base import (
    AmbiguousConverterError,
    ConverterMatch,
    ConverterMatchError,
    ConverterSelection,
    SourceConverter,
)
from app.core.source_conversion.converters import build_builtin_registry
from app.core.source_conversion.quality import encrypted_source_type
from app.core.source_conversion.registry import SourceConverterRegistry
from app.core.source_conversion.schemas import (
    CanonicalNote,
    ContentKind,
    ConversionDiagnostic,
    ConversionStatus,
    ConverterOutput,
    DiagnosticSeverity,
    RawSourceItem,
    SourceACLView,
    SourceIdentity,
    canonical_source_hash,
)

logger = logging.getLogger(__name__)

_SYSTEM_METADATA_KEYS = {
    "raw_acl", "acl", "acl_scope", "source_type", "source_id", "source_url",
    "source_path", "external_version", "source_hash", "body_hash", "converter",
    "converter_key", "converter_version", "conversion_status", "content_kind",
    "schema_version",
}

# Service 保留字段：ConverterOutput.conversion_metadata 不得覆盖
_RESERVED_METADATA_KEYS = {
    "selected_match", "candidate_matches", "content_kind", "converter_key",
    "converter_version", "conversion_status", "source_hash", "body_hash",
}


class CanonicalNoteService:
    def __init__(self, registry: SourceConverterRegistry | None = None):
        self.registry = registry if registry is not None else build_builtin_registry()

    def convert(self, item: RawSourceItem) -> CanonicalNote:
        encrypted = self._detect_encrypted(item)
        if encrypted:
            return self._build_blocked(item, (
                ConversionDiagnostic(
                    code="encrypted_source", severity=DiagnosticSeverity.ERROR,
                    message="检测到加密文件容器，需要由有权限的安全终端导出解密原文件后再同步",
                    detail={"encryption": encrypted},
                ),
            ))

        try:
            selection = self.registry.select(item)
        except AmbiguousConverterError as exc:
            logger.warning("source_conversion ambiguous: %s", exc)
            return self._build_blocked(item, (
                ConversionDiagnostic(
                    code="ambiguous_converter", severity=DiagnosticSeverity.ERROR,
                    message="多个转换器同时匹配且无法确定选择",
                ),
            ))
        except ConverterMatchError as exc:
            logger.error("converter match fail closed: key=%s error_type=%s", exc.converter_key, exc.error_type)
            return self._build_match_failed(item, exc)

        if selection is None:
            return self._build_blocked(item, (
                ConversionDiagnostic(
                    code="unsupported_format", severity=DiagnosticSeverity.ERROR,
                    message=f"未找到支持该格式的转换器 (extension={item.extension or '未知'}, "
                            f"mime={item.mime_type or '未知'}, content_kind={item.content_kind.value})",
                ),
            ))

        # Selection 后的重新验证 / 契约校验也可能抛 ConverterMatchError → 统一 blocked
        try:
            return self._convert_with_converter(item, selection)
        except ConverterMatchError as exc:
            logger.error("converter post-selection fail closed: key=%s error_type=%s", exc.converter_key, exc.error_type)
            return self._build_match_failed(item, exc)

    def _convert_with_converter(self, item: RawSourceItem, selection: ConverterSelection) -> CanonicalNote:
        converter: SourceConverter = selection.selected_converter
        selected_match: ConverterMatch = selection.selected_match
        # 从 Registry 校验获取规范化值（注册后篡改 → ConverterMatchError fail closed）
        try:
            from app.core.source_conversion.registry import _validate_converter
            converter_key, converter_version, _, output_kind = _validate_converter(converter)
        except ValueError as exc:
            logger.error("converter 校验失败（可能被篡改）: %s", exc)
            raise ConverterMatchError(
                getattr(converter, "key", "?"), "invalid_converter_after_registration", str(exc)
            ) from exc

        conversion_metadata: dict = {
            "selected_match": selected_match.to_dict(),
            "candidate_matches": [m.to_dict() for m in selection.candidate_matches],
        }

        try:
            output = converter.convert(item, selected_match)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except ValueError:
            logger.error("converter %s 契约违规（ValueError），traceback:\n%s", converter_key, traceback.format_exc())
            return self._contract_violation(
                item, converter_key, converter_version, conversion_metadata,
                output_kind, "转换器输出违反契约", code="converter_contract_violation",
            )
        except Exception:  # noqa: BLE001
            logger.error("converter %s 未预期异常，traceback:\n%s", converter_key, traceback.format_exc())
            return self._contract_violation(
                item, converter_key, converter_version, conversion_metadata,
                output_kind, "转换器执行失败", code="conversion_failed",
            )

        return self._assemble(item, converter_key, converter_version, output, conversion_metadata, output_kind)

    def _assemble(
        self,
        item: RawSourceItem,
        converter_key: str,
        converter_version: str,
        output: ConverterOutput,
        base_metadata: dict,
        output_kind: object | None,
    ) -> CanonicalNote:
        # ---- 非 ConverterOutput → contract_violation ----
        if not isinstance(output, ConverterOutput):
            logger.error("converter %s 返回非 ConverterOutput: %s", converter_key, type(output).__name__)
            return self._contract_violation(
                item, converter_key, converter_version, base_metadata,
                output_kind, "转换器未返回 ConverterOutput",
            )

        # ---- 保留字段保护：converter 不得覆盖 selected_match/candidate_matches 等 ----
        for k in output.conversion_metadata.keys():
            if k in _RESERVED_METADATA_KEYS:
                logger.error("converter %s 尝试覆盖保留字段 %s（contract_violation）", converter_key, k)
                return self._contract_violation(
                    item, converter_key, converter_version, base_metadata,
                    output_kind, f"转换器试图覆盖保留字段 {k}",
                )

        # ---- output_kind 校验：始终验证（UNKNOWN 不绕过） ----
        if output.content_kind != output_kind:
            logger.error(
                "converter %s 输出 content_kind=%s 与声明 output_kind=%s 不一致（contract_violation）",
                converter_key, output.content_kind.value, output_kind.value,
            )
            return self._contract_violation(
                item, converter_key, converter_version, base_metadata,
                output_kind, "转换器输出类型与声明不一致",
            )

        # ---- source_metadata_extra 系统字段保护：写系统字段 → contract_violation ----
        for key in output.source_metadata_extra.keys():
            if key in _SYSTEM_METADATA_KEYS and key != "input_frontmatter":
                logger.error("converter %s 尝试写入系统字段 %s（contract_violation）", converter_key, key)
                return self._contract_violation(
                    item, converter_key, converter_version, base_metadata,
                    output_kind, f"转换器试图写入系统字段 {key}",
                )

        # ---- 状态归一 ----
        status = self._normalize_status(
            body=output.body, signal=output.status_signal, diagnostics=output.diagnostics,
        )
        final_body = output.body or ""
        if status in (ConversionStatus.FAILED, ConversionStatus.BLOCKED):
            final_body = ""
        diagnostics = list(output.diagnostics)
        if status in (ConversionStatus.FAILED, ConversionStatus.BLOCKED) and not any(
            d.is_error for d in diagnostics
        ):
            diagnostics.append(ConversionDiagnostic(
                code="empty_body", severity=DiagnosticSeverity.ERROR,
                message="未提取到有效正文",
            ))

        # ---- conversion_metadata：保留字段由 Service 写入，converter 字段追加 ----
        conversion_metadata = dict(base_metadata)
        for k, v in (output.conversion_metadata or {}).items():
            if k not in _RESERVED_METADATA_KEYS:
                conversion_metadata[k] = v

        # ---- source_metadata ----
        source_metadata: dict = dict(thaw(item.source_metadata))
        for key, value in (output.source_metadata_extra or {}).items():
            source_metadata[key] = thaw(value)

        # ---- 标准 ACL / 来源身份 ----
        acl = item.acl if item.acl is not None else SourceACLView()
        identity = SourceIdentity(
            source_type=item.source_type,
            source_id=item.external_id,
            source_url=item.source_url,
            source_path=item.source_path,
            external_version=item.external_version,
        )
        title = output.suggested_title if (not (item.title or "").strip()) and output.suggested_title else (item.title or "")
        # content_kind 用已验证的 converter.output_kind（不再因 item.content_kind 产生二次矛盾）
        content_kind = output_kind

        note = CanonicalNote(
            body=final_body,
            title=title,
            identity=identity,
            source_metadata=source_metadata,
            acl=acl,
            converter_key=converter_key,
            converter_version=converter_version,
            conversion_status=status,
            diagnostics=tuple(diagnostics),
            conversion_metadata=conversion_metadata,
            source_hash=item.source_hash,
            content_kind=content_kind,
        )
        return note

    def _contract_violation(
        self,
        item: RawSourceItem,
        converter_key: str,
        converter_version: str,
        base_metadata: dict,
        output_kind: object | None,
        message: str,
        *,
        code: str = "converter_contract_violation",
    ) -> CanonicalNote:
        """契约违规：返回 failed Note，保留真实 selected_match/candidate_matches。"""
        output_kind = output_kind if isinstance(output_kind, ContentKind) else ContentKind.UNKNOWN
        acl = item.acl if item.acl is not None else SourceACLView()
        identity = SourceIdentity(
            source_type=item.source_type,
            source_id=item.external_id,
            source_url=item.source_url,
            source_path=item.source_path,
            external_version=item.external_version,
        )
        return CanonicalNote(
            body="",
            title=item.title or "",
            identity=identity,
            source_metadata=dict(thaw(item.source_metadata)),
            acl=acl,
            converter_key=converter_key,
            converter_version=converter_version,
            conversion_status=ConversionStatus.FAILED,
            diagnostics=(ConversionDiagnostic(
                code=code, severity=DiagnosticSeverity.ERROR,
                message=message,
            ),),
            conversion_metadata=dict(base_metadata),  # 保留真实 selected_match/candidate_matches
            source_hash=item.source_hash,
            content_kind=output_kind,
        )

    def _normalize_status(
        self,
        *,
        body: str,
        signal: ConversionStatus | None,
        diagnostics: tuple[ConversionDiagnostic, ...],
    ) -> ConversionStatus:
        has_body = bool((body or "").strip())
        has_error = any(d.severity == DiagnosticSeverity.ERROR for d in diagnostics)
        has_warning = any(d.severity == DiagnosticSeverity.WARNING for d in diagnostics)

        if not has_body:
            return ConversionStatus.FAILED
        if signal == ConversionStatus.FAILED:
            return ConversionStatus.FAILED
        if has_error:
            return ConversionStatus.PARTIAL
        if has_warning:
            return ConversionStatus.PARTIAL
        if signal == ConversionStatus.PARTIAL:
            return ConversionStatus.PARTIAL
        return ConversionStatus.CONVERTED

    def _build_blocked(self, item: RawSourceItem, diagnostics: tuple[ConversionDiagnostic, ...]) -> CanonicalNote:
        source_metadata: dict = dict(thaw(item.source_metadata))
        return CanonicalNote(
            body="",
            title=item.title or "",
            identity=SourceIdentity(
                source_type=item.source_type,
                source_id=item.external_id,
                source_url=item.source_url,
                source_path=item.source_path,
                external_version=item.external_version,
            ),
            source_metadata=source_metadata,
            acl=item.acl if item.acl is not None else SourceACLView(),
            converter_key="",
            converter_version="",
            conversion_status=ConversionStatus.BLOCKED,
            diagnostics=diagnostics,
            conversion_metadata={},
            source_hash=canonical_source_hash(item.payload),
            content_kind=item.content_kind,
        )

    def _build_match_failed(self, item: RawSourceItem, exc: ConverterMatchError) -> CanonicalNote:
        """统一 ConverterMatchError → blocked + converter_match_failed diagnostic。"""
        return self._build_blocked(item, (
            ConversionDiagnostic(
                code="converter_match_failed", severity=DiagnosticSeverity.ERROR,
                message="转换器匹配阶段失败（已 fail closed）",
                detail={"converter_key": exc.converter_key, "error_type": exc.error_type},
            ),
        ))

    def _detect_encrypted(self, item: RawSourceItem) -> str:
        if item.raw_bytes:
            return encrypted_source_type(item.raw_bytes)
        return ""

    def register(self, converter: SourceConverter, *, replace: bool = False) -> None:
        self.registry.register(converter, replace=replace)

    def registered_keys(self) -> list[str]:
        return self.registry.keys()


def thaw(value):
    from app.core.source_conversion.schemas import thaw_value
    return thaw_value(value)
