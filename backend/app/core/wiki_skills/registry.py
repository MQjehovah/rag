"""Phase 6：Skill Registry（版本 + Runtime 注册，allowlist + 显式 active）。

- 同 key/version + 完整定义一致 → 幂等 no-op；
- 同 key/version + 定义不同 → SkillDefinitionConflict；
- 同 key 多版本 → 允许共存；
- active version：显式管理（set_active_version），不依赖注册顺序；指向已注册版本；
  缺失/删除 active → fail closed（get_active 返回 None，get 抛 SkillNotFound）；
- Runtime：runtime_key 必须来自代码侧 allowlist；descriptor 不保存 callable；
  runtime 的 key/version 必须与 descriptor 对齐；注册后修改原对象不影响内部状态
  （保存时深拷贝冻结）。
- snapshot：真正深层不可变（MappingProxyType + tuple），各层原地修改抛 TypeError；
  key/version 均稳定排序输出（不依赖注册先后）；不暴露内部可变容器/对象引用。
- list_skills / registered_versions：稳定排序，不依赖注册顺序。

错误统一为受控 SkillError 子类（不使用 KeyError/AttributeError 泄漏实现细节）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from app.core.wiki_skills.base import SkillRuntime
from app.core.wiki_skills.schemas import SkillDescriptor, _freeze


class SkillError(RuntimeError):
    """Skill 框架受控异常基类。"""


class SkillNotFound(SkillError):
    """未知 skill key/version。"""


class SkillDefinitionConflict(SkillError):
    """同 key/version 定义冲突。"""


class SkillRuntimeError(SkillError):
    """Runtime 注册/对齐错误。"""


class SkillActiveVersionError(SkillError):
    """active version 指向未注册版本。"""


# skill_key → {version: SkillDescriptor}（按注册顺序）。
_DESCRIPTORS: dict[str, dict[str, SkillDescriptor]] = {}
# skill_key → {version: SkillRuntime}。
_RUNTIMES: dict[str, dict[str, SkillRuntime]] = {}
# skill_key → active version（显式）。
_ACTIVE: dict[str, str] = {}
# runtime_key → 代码侧 allowlist（可调用工厂/类）。
_RUNTIME_ALLOWLIST: dict[str, Callable] = {}


def register_runtime_allowlist(runtime_key: str, factory: Callable) -> None:
    """把 Python 侧 Runtime 工厂/类登记进 allowlist（受控本地代码，非远程）。"""
    if not isinstance(runtime_key, str) or not runtime_key.strip():
        raise SkillRuntimeError("runtime_key required")
    if not callable(factory):
        raise SkillRuntimeError("factory must be callable")
    _RUNTIME_ALLOWLIST[runtime_key.strip()] = factory


def register_descriptor(descriptor: SkillDescriptor) -> None:
    """注册 descriptor。同 key/version 定义一致 → 幂等；不一致 → 冲突。"""
    if not isinstance(descriptor, SkillDescriptor):
        raise SkillError("register_descriptor requires SkillDescriptor")
    versions = _DESCRIPTORS.setdefault(descriptor.key, {})
    existing = versions.get(descriptor.version)
    if existing is not None:
        if existing.to_dict() == descriptor.to_dict():
            return  # 幂等 no-op
        raise SkillDefinitionConflict(
            f"skill_definition_conflict={descriptor.key}:{descriptor.version}"
        )
    versions[descriptor.version] = descriptor


def register_runtime(runtime: SkillRuntime) -> None:
    """注册 Runtime。runtime_key 必须 allowlist；key/version 与 descriptor 对齐。"""
    if not isinstance(runtime, SkillRuntime):
        raise SkillRuntimeError("register_runtime requires SkillRuntime")
    key = runtime.key
    version = runtime.version
    descriptor = _DESCRIPTORS.get(key, {}).get(version)
    if descriptor is None:
        raise SkillRuntimeError(
            f"skill_runtime_missing_descriptor={key}:{version}"
        )
    factory = _RUNTIME_ALLOWLIST.get(descriptor.runtime_key)
    if factory is None:
        raise SkillRuntimeError(
            f"runtime_key_not_allowed={descriptor.runtime_key}"
        )
    if not isinstance(runtime, factory):
        raise SkillRuntimeError(
            f"skill_runtime_type_mismatch={key}:{version}"
        )
    versions = _RUNTIMES.setdefault(key, {})
    existing = versions.get(version)
    if existing is not None:
        if existing is runtime:
            return  # 幂等（同一实例）
        if type(existing) is type(runtime):
            # 同类新实例（如重复 startup 注册）视为同定义幂等替换，非冲突。
            versions[version] = runtime
            return
        raise SkillRuntimeError(f"skill_runtime_conflict={key}:{version}")
    versions[version] = runtime


def get(key: str, version: str | None = None) -> SkillDescriptor:
    """精确/active 查询。version=None → active；缺失 → SkillNotFound。"""
    versions = _DESCRIPTORS.get(key)
    if not versions:
        raise SkillNotFound(f"skill_not_found={key}")
    if version is None:
        active = _ACTIVE.get(key)
        if active is not None and active in versions:
            return versions[active]
        raise SkillNotFound(f"skill_not_found={key} (no active)")
    desc = versions.get(version)
    if desc is None:
        raise SkillNotFound(f"skill_not_found={key}:{version}")
    return desc


def get_runtime(key: str, version: str) -> SkillRuntime:
    """精确查询 Runtime（返回对象供调用方使用，内部状态不受外部修改影响）。"""
    versions = _RUNTIMES.get(key)
    if not versions or version not in versions:
        raise SkillNotFound(f"skill_runtime_not_found={key}:{version}")
    return versions[version]


def get_active(key: str) -> SkillDescriptor | None:
    """显式 active descriptor；未设置/已删除 → None（fail closed，不猜测）。"""
    versions = _DESCRIPTORS.get(key)
    if not versions:
        return None
    active = _ACTIVE.get(key)
    if active is not None and active in versions:
        return versions[active]
    return None


def set_active_version(key: str, version: str) -> None:
    """显式设置 active version。必须指向已注册版本，否则 fail closed。"""
    versions = _DESCRIPTORS.get(key)
    if not versions or version not in versions:
        raise SkillActiveVersionError(
            f"skill_active_version_not_registered={key}:{version}"
        )
    _ACTIVE[key] = version


def _sorted_versions(versions: dict) -> list[str]:
    """version 稳定排序输出（不依赖注册先后）。"""
    return sorted(versions.keys())


def list_skills() -> list[dict]:
    """稳定排序的 skill 摘要列表（key 排序，version 稳定排序）。"""
    out: list[dict] = []
    for key in sorted(_DESCRIPTORS.keys()):
        versions = _DESCRIPTORS[key]
        active = _ACTIVE.get(key)
        out.append({
            "key": key,
            "active_version": active if active in versions else None,
            "versions": _sorted_versions(versions),
        })
    return out


def snapshot() -> Mapping:
    """真正深层不可变的注册快照（不暴露内部可变字典/对象引用）。

    - 顶层 / Skill 信息层 / versions 层 / descriptor 字段层 / applicability_signals
      均为不可变 Mapping（MappingProxyType）或 tuple；
    - key 与 version 均稳定排序（不依赖注册顺序）；
    - 对任一返回层原地修改都抛 TypeError。
    """
    out: dict = {}
    for key in sorted(_DESCRIPTORS.keys()):
        versions = _DESCRIPTORS[key]
        active = _ACTIVE.get(key)
        out[key] = _freeze({
            "active_version": active if active in versions else None,
            "versions": {
                v: _freeze(versions[v].to_dict())
                for v in _sorted_versions(versions)
            },
        })
    return _freeze(out)


def get_runtime_factory(runtime_key: str):
    """查询 allowlist 中的 Runtime 工厂/类（未登记 → None）。"""
    return _RUNTIME_ALLOWLIST.get(runtime_key)


def has(key: str, version: str) -> bool:
    return key in _DESCRIPTORS and version in _DESCRIPTORS[key]


def registered_versions(key: str) -> list[str]:
    return _sorted_versions(_DESCRIPTORS.get(key, {}))


def clear_for_tests() -> None:
    """测试隔离：清空全部注册状态（含 allowlist？否——allowlist 是静态代码映射）。"""
    _DESCRIPTORS.clear()
    _RUNTIMES.clear()
    _ACTIVE.clear()
