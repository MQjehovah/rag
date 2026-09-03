"""Phase 6：Loader（受控本地资源加载，只加载 builtin/ 目录）。

安全规则：
- 根目录 resolve：backend/app/core/wiki_skills/builtin/；
- 每个资源路径 resolve 后必须仍位于 Skill 根目录（阻止 ../、绝对路径、盘符、
  UNC、符号链接逃逸）；
- skill.yaml 严格字段白名单（= SkillDescriptor 字段），拒绝任何可执行 Python
  路径/import/callable/URL；
- JSON Schema 必须 JSON object + 稳定 $id/内部 id + "type": "object"；非法 JSON
  拒绝；非 object 拒绝。当前环境**无 jsonschema 依赖**（不安装新依赖），只做本
  阶段必要结构校验（如环境已有 jsonschema 则走标准 check_schema）；
- instructions.md 受控文本：最大字节限制；不可执行；API 不直接返回全文；
- 非 UTF-8 声明文件拒绝；
- 非 default Skill 损坏 → 隔离该 Skill + 记录安全诊断，default 仍可用；
- default 损坏 → 抛错（启动失败，不允许无 default 继续）。

本模块不 import/执行任何 runtime 代码（runtime 经 registry allowlist 显式注册）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.core.wiki_skills.registry import SkillError
from app.core.wiki_skills.schemas import SkillDescriptor

# skill.yaml 严格白名单（与 SkillDescriptor 字段一致；不含任何可执行路径）。
_YAML_ALLOWED_FIELDS = {
    "key", "version", "label", "description", "applicability_signals",
    "extraction_schema_id", "blueprint_schema_id", "instruction_resource",
    "runtime_key",
}

# 禁止出现在 YAML 中（防误配置注入可执行路径/callable）。
_YAML_FORBIDDEN_KEYS = {
    "python_path", "module", "callable", "remote_url", "download_url",
    "script", "command", "import", "exec", "eval",
}

# 字段长度上限（防超长资源）。
_MAX_INSTRUCTION_BYTES = 200_000
_MAX_SCHEMA_BYTES = 100_000
_MAX_YAML_BYTES = 32_000


class LoaderError(SkillError):
    """Loader 受控错误基类。"""


class SkillLoadError(LoaderError):
    """Skill 目录加载失败（结构/字段/资源）。"""


class SkillDefaultError(SkillError):
    """default Skill 加载失败（启动必须中止）。"""


def resolve_builtin_root() -> Path:
    """返回受控 builtin 根目录（resolve 后）。"""
    root = Path(__file__).resolve().parent / "builtin"
    return root


def _read_bytes(path: Path, max_bytes: int, label: str) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillLoadError(f"{label} unreadable: {path.name}") from exc
    if len(raw) > max_bytes:
        raise SkillLoadError(f"{label} exceeds size limit: {path.name}")
    return raw


def _read_utf8(path: Path, max_bytes: int, label: str) -> str:
    raw = _read_bytes(path, max_bytes, label)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillLoadError(f"{label} not utf-8: {path.name}") from exc


def _safe_join(root: Path, relative: str) -> Path:
    """解析相对资源路径并强制约束在 root 内（含符号链接逃逸检测）。"""
    if not isinstance(relative, str) or not relative.strip():
        raise SkillLoadError("empty resource path")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise SkillLoadError(f"absolute path not allowed: {relative}")
    # Windows 盘符（如 "C:foo"）与 UNC（"\\\\server\\..."）判为非法。
    text = relative.strip()
    if os.path.splitdrive(text)[0]:
        raise SkillLoadError(f"drive path not allowed: {relative}")
    if text.startswith("\\\\") or text.startswith("//"):
        raise SkillLoadError(f"UNC path not allowed: {relative}")
    resolved = (root / candidate).resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise SkillLoadError(f"path escapes skill root: {relative}") from exc
    # 符号链接逃逸：realpath 后仍在 root 内（resolve() 已含 realpath 语义）。
    real_resolved = os.path.realpath(resolved)
    real_root = os.path.realpath(root_resolved)
    if not real_resolved.startswith(real_root + os.sep) and real_resolved != real_root:
        raise SkillLoadError(f"symlink escape not allowed: {relative}")
    return resolved


def _parse_yaml_safe(text: str, source: str) -> dict:
    import yaml

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"invalid yaml in {source}") from exc
    if not isinstance(loaded, dict):
        raise SkillLoadError(f"skill.yaml must be a mapping: {source}")
    return loaded


def _validate_yaml_fields(data: dict, source: str) -> None:
    unknown = set(data.keys()) - _YAML_ALLOWED_FIELDS
    if unknown:
        raise SkillLoadError(f"unknown yaml fields {sorted(unknown)} in {source}")
    forbidden = set(data.keys()) & _YAML_FORBIDDEN_KEYS
    if forbidden:
        raise SkillLoadError(f"forbidden yaml fields {sorted(forbidden)} in {source}")
    for value in data.values():
        # 只允许标量 / 字符串列表（拒绝嵌套 dict / 任何可执行形态）。
        if isinstance(value, (dict, list)):
            if isinstance(value, list):
                if not all(isinstance(v, str) for v in value):
                    raise SkillLoadError(f"non-string list value in {source}")
                continue
            raise SkillLoadError(f"nested mapping not allowed in {source}")
        if not isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                raise SkillLoadError(f"unsupported scalar type in {source}")


def _load_json_schema(path: Path) -> dict:
    """JSON Schema 加载与必要结构校验（无 jsonschema 时退化为结构校验）。"""
    if path.name == "skill.yaml":
        raise SkillLoadError("schema id mismatch")
    text = _read_utf8(path, _MAX_SCHEMA_BYTES, "schema")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SkillLoadError(f"invalid json schema: {path.name}") from exc
    if not isinstance(data, dict):
        raise SkillLoadError(f"schema must be json object: {path.name}")
    # 必须为 JSON Schema object 且声明 object 根。
    if data.get("type") != "object":
        raise SkillLoadError(f"schema type must be object: {path.name}")
    has_id = isinstance(data.get("$id"), str) and bool(data["$id"])
    has_internal_id = isinstance(data.get("id"), str) and bool(data["id"])
    has_title = isinstance(data.get("title"), str) and bool(data["title"])
    if not (has_id or has_internal_id):
        raise SkillLoadError(f"schema missing stable $id/id: {path.name}")
    try:
        import jsonschema  # noqa: F401

        jsonschema.validators.validator_for(data).check_schema(data)
    except ImportError:
        # 环境无 jsonschema：不安装新依赖，仅完成上述必要结构校验。
        pass
    return data


def _load_json(path: Path) -> dict:
    if not path.is_file():
        raise SkillLoadError(f"missing resource: {path.name}")
    return _load_json_schema(path)


def _load_markdown(path: Path) -> str:
    if not path.is_file():
        raise SkillLoadError(f"missing resource: {path.name}")
    text = _read_utf8(path, _MAX_INSTRUCTION_BYTES, "instructions")
    return text


# 每个 Skill 目录的必需资源名。
_REQUIRED_RESOURCES = (
    "skill.yaml",
    "extraction.schema.json",
    "blueprint.schema.json",
    "instructions.md",
    "runtime.py",
)


def _ensure_skill_dir(skill_dir) -> Path:
    """校验 skill_dir 是 builtin 根目录的直接 Skill 子目录（resolve 后仍位于根内）。

    拒绝：根外目录 / 绝对外部目录 / `..` 逃逸 / UNC、盘符逃逸 / 指向根外的目录
    符号链接 / builtin 根下深层任意目录。
    """
    if not isinstance(skill_dir, Path):
        raise SkillLoadError("skill_dir must be a Path")
    root = resolve_builtin_root()
    root_res = root.resolve()
    try:
        dir_res = skill_dir.resolve()
    except OSError as exc:  # 悬空链接/不可解析
        raise SkillLoadError("skill dir cannot be resolved") from exc
    try:
        dir_res.relative_to(root_res)
    except ValueError as exc:
        raise SkillLoadError(f"skill dir outside builtin root: {skill_dir}") from exc
    if dir_res.parent != root_res:
        raise SkillLoadError(
            f"skill dir must be a direct child of builtin root: {skill_dir}"
        )
    if not dir_res.is_dir():
        raise SkillLoadError(f"skill dir is not a directory: {skill_dir}")
    return dir_res


def _schema_declared_id(data: dict) -> str:
    """解析 JSON Schema 的稳定 id。

    规则：`$id` 与 `id` 同时出现时必须相等；否则取出现者；都不出现 → ""。
    冲突 → SkillLoadError。
    """
    dollar = data.get("$id")
    plain = data.get("id")
    dollar_ok = isinstance(dollar, str) and bool(dollar.strip())
    plain_ok = isinstance(plain, str) and bool(plain.strip())
    if dollar_ok and plain_ok:
        if dollar.strip() != plain.strip():
            raise SkillLoadError(f"schema $id/id conflict: {dollar} vs {plain}")
        return dollar.strip()
    if dollar_ok:
        return dollar.strip()
    if plain_ok:
        return plain.strip()
    return ""


def _check_schema_alignment(declared: str, schema_id: str, kind: str, key: str) -> None:
    """schema 声明的 $id/id 必须与 YAML 的 *_schema_id 精确一致。"""
    declared = (declared or "").strip()
    if not declared or not schema_id or declared != schema_id:
        raise SkillLoadError(
            f"{kind} schema id mismatch in {key}: yaml={declared!r} schema={schema_id!r}"
        )


def _load_descriptor_from_dir(skill_dir: Path) -> SkillDescriptor:
    """从单个 Skill 目录加载并校验 descriptor（不读取/执行 runtime.py 内容）。"""
    _ensure_skill_dir(skill_dir)
    yaml_path = skill_dir / "skill.yaml"
    if not yaml_path.is_file():
        raise SkillLoadError(f"missing skill.yaml in {skill_dir.name}")
    text = _read_utf8(yaml_path, _MAX_YAML_BYTES, "skill.yaml")
    data = _parse_yaml_safe(text, skill_dir.name)
    _validate_yaml_fields(data, skill_dir.name)
    # key/version 等基础字段由 SkillDescriptor 构造时强校验。
    key = data.get("key", "")
    version = str(data.get("version", ""))
    # 子目录名必须与 YAML key 一致（防目录与声明错位）。
    if skill_dir.name != key:
        raise SkillLoadError(
            f"skill dir name mismatch: dir={skill_dir.name} yaml_key={key}"
        )
    descriptor = SkillDescriptor(
        key=key,
        version=version,
        label=str(data.get("label", "")),
        description=str(data.get("description", "")),
        applicability_signals=tuple(
            str(s) for s in (data.get("applicability_signals") or [])
        ),
        extraction_schema_id=str(data.get("extraction_schema_id", "")),
        blueprint_schema_id=str(data.get("blueprint_schema_id", "")),
        instruction_resource=str(data.get("instruction_resource", "")),
        runtime_key=str(data.get("runtime_key", "")),
    )
    # 全部必需资源存在性（runtime.py 只检查是普通文件：不读内容、不 import、不执行）。
    for res_name in _REQUIRED_RESOURCES:
        res_path = skill_dir / res_name
        if not res_path.is_file():
            raise SkillLoadError(f"missing resource {res_name} in {key}")
    # instruction_resource：声明文件必须存在且位于 skill 目录内（绝对/../ 逃逸拒绝）。
    instr_rel = descriptor.instruction_resource
    if instr_rel:
        instr_path = _safe_join(skill_dir, instr_rel)
        if not instr_path.is_file():
            raise SkillLoadError(f"instruction resource missing: {instr_rel}")
    # schema JSON 解析 + 结构校验 + $id/id 与 YAML 对齐。
    ext_data = _load_json(skill_dir / "extraction.schema.json")
    bp_data = _load_json(skill_dir / "blueprint.schema.json")
    _check_schema_alignment(
        descriptor.extraction_schema_id, _schema_declared_id(ext_data),
        "extraction", key,
    )
    _check_schema_alignment(
        descriptor.blueprint_schema_id, _schema_declared_id(bp_data),
        "blueprint", key,
    )
    # instructions.md 大小/编码校验。
    _load_markdown(skill_dir / "instructions.md")
    return descriptor


def _skill_dirs() -> list[Path]:
    root = resolve_builtin_root()
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and (p / "skill.yaml").is_file()),
        key=lambda p: p.name,
    )


def load_skill(skill_dir: Path) -> SkillDescriptor:
    """加载单个 Skill 目录（default 之外的损坏须由调用方隔离）。"""
    return _load_descriptor_from_dir(skill_dir)


def load_all() -> dict[str, dict[str, SkillDescriptor]]:
    """加载 builtin/ 全部 Skill（key → version → descriptor）。

    - default 加载失败 → SkillDefaultError（启动必须中止）；
    - 其它 Skill 加载失败 → 隔离该 Skill，记录安全诊断，default 仍可用。
    返回成功加载的 descriptor 嵌套 dict；诊断经 load_diagnostics() 读取。
    """
    _last_diagnostics.clear()
    result: dict[str, dict[str, SkillDescriptor]] = {}
    for skill_dir in _skill_dirs():
        key = skill_dir.name
        try:
            descriptor = _load_descriptor_from_dir(skill_dir)
        except SkillDefaultError:
            raise
        except LoaderError as exc:
            if key == "default":
                raise SkillDefaultError(f"default skill load failed: {exc}") from exc
            _last_diagnostics.append({"skill": key, "error": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001
            if key == "default":
                raise SkillDefaultError(f"default skill load failed: {exc}") from exc
            _last_diagnostics.append({"skill": key, "error": "unexpected load error"})
            continue
        if descriptor.key != "default" and descriptor.key != key:
            _last_diagnostics.append({"skill": key, "error": "key mismatch"})
            continue
        result.setdefault(key, {})[descriptor.version] = descriptor
    if "default" not in result:
        raise SkillDefaultError("default skill missing after load_all")
    return result


_last_diagnostics: list[dict] = []


def load_diagnostics() -> list[dict]:
    """最近一次 load_all 的隔离诊断（安全：不含原始文件内容/路径细节）。"""
    return [dict(d) for d in _last_diagnostics]


def load_builtin_default() -> SkillDescriptor:
    """加载 default Skill（失败 → SkillDefaultError，禁止无 default 继续）。"""
    root = resolve_builtin_root()
    default_dir = root / "default"
    try:
        return _load_descriptor_from_dir(default_dir)
    except SkillDefaultError:
        raise
    except LoaderError as exc:
        raise SkillDefaultError(f"default skill load failed: {exc}") from exc
