"""阶段 2 技能存储：不可变指令版本 + Workspace/domain 绑定 + 冻结解析。

- 内容不可变：只新增版本（seq 单调递增），无更新 API；重复导入语义明确：
  builtin/manual 种子同内容幂等复用；add-version 始终新建（可测）。
- Runtime 与指令分离：技能包只携带 runtime_ref（兼容标识）；进程级 Registry/
  代码仍是受控 Runtime。
- 排序/比较用 seq（整数），不依赖字符串大小。
- 冻结：resolve_freeze 校验版本存在、内容哈希一致、domain/Runtime 兼容，
  产出 FrozenSkillSet（含注入文本、版本/哈希/set 哈希）；缺失/损坏/不兼容
  → SkillStoreError，不静默替换。
- 绑定：kind=experiment/business（业务默认不启用）；无行=功能关闭；
  set_kind='empty' 行=显式空技能集合。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.skill_evolution.contracts import canonical_json
from app.core.skill_evolution.errors import SkillPackageError, SkillStoreError
from app.core.skill_evolution.injector import FrozenSkillSet
from app.models.evolution import (
    EVOLUTION_SCHEMA_VERSION,
    SET_KIND_EMPTY,
    SET_KIND_SKILL,
    SOURCE_TYPE_BUILTIN_SEED,
    SOURCE_TYPE_MANUAL_SEED,
    EvolutionSkillBinding,
    EvolutionSkillVersion,
)

# 本阶段受控兼容标记：default 通用 Wiki 编译内容生成 Runtime。
RUNTIME_REF = "wiki.compile.default.runtime/v1"
DEFAULT_DOMAIN = "wiki_compile.default"
PACKAGE_SCHEMA_VERSION = "1"
# skill_id 白名单由 domain+runtime_ref 把关；本阶段只支持 default 编译领域，
# 但提议者可为该领域提出新的技能身份（create），故不锁定单一字符串集合。

STORE_FILENAME = "skill_store.db"

_MAX_SKILL_BYTES = 60_000
_MAX_PURPOSE_BYTES = 40_000

# 种子目录（仓库内受控技能包，人工整理）。
SEED_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "eval" / "wiki_evolution" / "skills" / "seed-default-v1"
)

_REQUIRED_SKILL_SECTIONS = ("适用条件", "不适用条件", "操作步骤")
_REQUIRED_PURPOSE_SECTIONS = ("来源", "改进目的", "演化历史")

VERSION_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*:\d{4}$")


# ---------------------------------------------------------------------------
# 技能包
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillPackage:
    skill_id: str
    domain: str
    runtime_ref: str
    schema_version: str
    skill_md: str
    purpose_md: str
    source_type: str
    origin: str = ""

    def content_payload(self) -> dict:
        # 内容哈希以“入库后行级 schema 常量”为准（见 _recompute_hash），
        # 保证写入前后一致，不把包内 schema_version 字符串混入。
        return {
            "skill_id": self.skill_id,
            "domain": self.domain,
            "runtime_ref": self.runtime_ref,
            "schema_version": EVOLUTION_SCHEMA_VERSION,
            "skill_md": self.skill_md,
            "purpose_md": self.purpose_md,
        }

    def content_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.content_payload()).encode("utf-8")).hexdigest()


def _read_limited(path: Path, max_bytes: int, label: str) -> str:
    if not path.is_file():
        raise SkillPackageError(f"技能包缺少 {label}（{path.name}）")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise SkillPackageError(f"技能包 {label} 超限（>{max_bytes} 字节）")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillPackageError(f"技能包 {label} 非 UTF-8") from exc


def build_package_from_texts(skill_md: str, purpose_md: str, *,
                             source_type: str, origin: str = "") -> SkillPackage:
    """从文本构造并校验技能包（不依赖目录/文件）；提议者补丁与 create 共用。"""
    if source_type not in (SOURCE_TYPE_BUILTIN_SEED, SOURCE_TYPE_MANUAL_SEED):
        raise SkillPackageError(f"非法 source_type: {source_type}")
    if len(skill_md.encode("utf-8")) > _MAX_SKILL_BYTES:
        raise SkillPackageError(f"SKILL.md 超限（>{_MAX_SKILL_BYTES} 字节）")
    if len(purpose_md.encode("utf-8")) > _MAX_PURPOSE_BYTES:
        raise SkillPackageError(f"PURPOSE.md 超限（>{_MAX_PURPOSE_BYTES} 字节）")
    meta = _parse_frontmatter(skill_md)
    skill_id = str(meta.get("skill_id") or "").strip()
    domain = str(meta.get("domain") or "").strip()
    runtime_ref = str(meta.get("runtime_ref") or "").strip()
    schema_version = str(meta.get("schema_version") or "").strip()
    if domain != DEFAULT_DOMAIN:
        raise SkillPackageError(
            f"绑定/注入暂不支持 domain={domain!r}（仅 {DEFAULT_DOMAIN!r}；"
            "API Reference 等结构化技能须先扩展注入边界，不得静默忽略）")
    if runtime_ref != RUNTIME_REF:
        raise SkillPackageError(f"Runtime 不兼容: {runtime_ref!r} != {RUNTIME_REF!r}")
    if schema_version != PACKAGE_SCHEMA_VERSION:
        raise SkillPackageError(f"schema_version 不支持: {schema_version!r}")
    _require_sections(skill_md, _REQUIRED_SKILL_SECTIONS, "SKILL.md")
    _require_sections(purpose_md, _REQUIRED_PURPOSE_SECTIONS, "PURPOSE.md")
    return SkillPackage(
        skill_id=skill_id, domain=domain, runtime_ref=runtime_ref,
        schema_version=schema_version, skill_md=skill_md, purpose_md=purpose_md,
        source_type=source_type, origin=origin,
    )


def load_package(skill_dir: Path, *, source_type: str) -> SkillPackage:
    """加载并校验 SKILL.md/PURPOSE.md 文件（委托 build_package_from_texts）。"""
    skill_dir = Path(skill_dir).resolve()
    skill_md = _read_limited(skill_dir / "SKILL.md", _MAX_SKILL_BYTES, "SKILL.md")
    purpose_md = _read_limited(skill_dir / "PURPOSE.md", _MAX_PURPOSE_BYTES, "PURPOSE.md")
    return build_package_from_texts(
        skill_md, purpose_md, source_type=source_type, origin=str(skill_dir))


def _parse_frontmatter(text: str) -> dict:
    stripped = text.lstrip("\ufeff \t")
    if not stripped.startswith("```yaml"):
        raise SkillPackageError("SKILL.md 必须以 ```yaml 元数据块开头")
    end = stripped.find("```", len("```yaml"))
    if end < 0:
        raise SkillPackageError("SKILL.md YAML 元数据块未闭合")
    body = stripped[len("```yaml"):end]
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise SkillPackageError("缺少 PyYAML") from exc
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise SkillPackageError(f"SKILL.md YAML 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillPackageError("SKILL.md YAML 元数据必须是对象")
    return data


def _require_sections(text: str, headings: tuple[str, ...], label: str) -> None:
    missing = [h for h in headings if h not in text]
    if missing:
        raise SkillPackageError(f"{label} 缺少必需段落: {missing}")


# ---------------------------------------------------------------------------
# 版本记录（只读）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillVersionRow:
    version_id: str
    skill_id: str
    seq: int
    schema_version: str
    domain: str
    runtime_ref: str
    parent_version_id: str | None
    skill_md: str
    purpose_md: str
    content_hash: str
    source_type: str
    created_by: str | None

    @classmethod
    def from_model(cls, row: EvolutionSkillVersion) -> "SkillVersionRow":
        return cls(
            version_id=row.version_id,
            skill_id=row.skill_id,
            seq=row.seq,
            schema_version=row.schema_version,
            domain=row.domain,
            runtime_ref=row.runtime_ref,
            parent_version_id=row.parent_version_id,
            skill_md=row.skill_md,
            purpose_md=row.purpose_md,
            content_hash=row.content_hash,
            source_type=row.source_type,
            created_by=row.created_by,
        )


def _recompute_hash(version_id: str, skill_id: str, domain: str, runtime_ref: str,
                    schema_version: str, skill_md: str, purpose_md: str) -> str:
    return hashlib.sha256(canonical_json({
        "skill_id": skill_id,
        "domain": domain,
        "runtime_ref": runtime_ref,
        "schema_version": EVOLUTION_SCHEMA_VERSION,
        "skill_md": skill_md,
        "purpose_md": purpose_md,
    }).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 存储操作（session 绑定实验/演化库）
# ---------------------------------------------------------------------------


def _store_engine(root: Path):
    """按 root 缓存 skill_store.db engine（进程内复用）。"""
    from app.core.skill_evolution import runenv
    import threading
    cache = getattr(_store_engine, "_cache", None)
    if cache is None:
        cache = {}
        _store_engine._cache = cache  # type: ignore[attr-defined]
    root = Path(root).resolve()
    key = str(root)
    lock = getattr(_store_engine, "_lock", None)
    if lock is None:
        lock = threading.Lock()
        _store_engine._lock = lock  # type: ignore[attr-defined]
    with lock:
        engine = cache.get(key)
        if engine is None:
            db_path = root / STORE_FILENAME
            engine = runenv.make_experiment_engine(db_path)
            # 演化 schema 显式创建（独立 metadata，不涉及业务表；业务库须 Alembic）。
            from app.models import evolution as _ev
            _ev.create_evolution_schema(engine)
            _ev.require_evolution_schema(engine, context=f"skill_store:{root}")
            cache[key] = engine
        return engine


def session_for(root: Path) -> Session:
    """返回绑定 root skill_store.db 的 session（调用方负责 close）。"""
    from sqlalchemy.orm import sessionmaker
    engine = _store_engine(root)
    Session = sessionmaker(bind=engine)
    return Session()


# 每个 skill 的下一 seq（不依赖字符串）。
def _next_seq(db: Session, skill_id: str) -> int:
    last = (
        db.query(EvolutionSkillVersion.seq)
        .filter(EvolutionSkillVersion.skill_id == skill_id)
        .order_by(EvolutionSkillVersion.seq.desc())
        .first()
    )
    return (last[0] + 1) if last else 1


def _version_id_for(skill_id: str, seq: int) -> str:
    return f"{skill_id}:{seq:04d}"


def _insert(db: Session, pkg: SkillPackage, *, parent_version_id: str | None,
            created_by: str | None, seq: int) -> EvolutionSkillVersion:
    row = EvolutionSkillVersion(
        version_id=_version_id_for(pkg.skill_id, seq),
        skill_id=pkg.skill_id,
        seq=seq,
        schema_version=EVOLUTION_SCHEMA_VERSION,
        domain=pkg.domain,
        runtime_ref=pkg.runtime_ref,
        parent_version_id=parent_version_id,
        skill_md=pkg.skill_md,
        purpose_md=pkg.purpose_md,
        content_hash=pkg.content_hash(),
        source_type=pkg.source_type,
        created_by=created_by,
    )
    db.add(row)
    return row


def find_version_by_content(db: Session, skill_id: str,
                            content_hash: str) -> str | None:
    """同 skill 同内容的历史版本（重复候选识别用）。"""
    row = (
        db.query(EvolutionSkillVersion)
        .filter(EvolutionSkillVersion.skill_id == skill_id,
                EvolutionSkillVersion.content_hash == content_hash)
        .order_by(EvolutionSkillVersion.seq.asc())
        .first()
    )
    return row.version_id if row is not None else None


def add_version_uncommitted(db: Session, pkg: SkillPackage, *,
                            parent_version_id: str | None,
                            created_by: str | None = None
                            ) -> EvolutionSkillVersion:
    """在调用方事务内新增版本行（不 commit；调用方统一提交避免孤儿候选）。"""
    if parent_version_id is not None:
        parent = get_version(db, parent_version_id)
        if parent.skill_id != pkg.skill_id:
            raise SkillStoreError(
                f"parent {parent_version_id} 的 skill_id 与新包不一致")
        if parent.domain != pkg.domain or parent.runtime_ref != pkg.runtime_ref:
            raise SkillStoreError("parent 与新增包的 domain/runtime_ref 不一致")
    return _insert(db, pkg, parent_version_id=parent_version_id,
                   created_by=created_by, seq=_next_seq(db, pkg.skill_id))


def import_seed(db: Session, pkg: SkillPackage) -> str:
    """导入种子技能；同 skill 同内容（同 content_hash+source_type）幂等复用。"""
    existing = (
        db.query(EvolutionSkillVersion)
        .filter(EvolutionSkillVersion.skill_id == pkg.skill_id,
                EvolutionSkillVersion.content_hash == pkg.content_hash(),
                EvolutionSkillVersion.source_type == pkg.source_type)
        .order_by(EvolutionSkillVersion.seq.asc())
        .first()
    )
    if existing is not None:
        return existing.version_id
    row = _insert(db, pkg, parent_version_id=None, created_by=None,
                  seq=_next_seq(db, pkg.skill_id))
    db.commit()
    return row.version_id


def add_version(db: Session, pkg: SkillPackage, *, parent_version_id: str | None,
                created_by: str | None = None) -> str:
    """显式新增版本（新内容 → 新 seq，绝不覆盖既有版本）。"""
    if parent_version_id is not None:
        parent = get_version(db, parent_version_id)
        if parent.skill_id != pkg.skill_id:
            raise SkillStoreError(
                f"parent {parent_version_id} 的 skill_id 与新包不一致")
        if parent.domain != pkg.domain or parent.runtime_ref != pkg.runtime_ref:
            raise SkillStoreError("parent 与新增包的 domain/runtime_ref 不一致")
    row = _insert(db, pkg, parent_version_id=parent_version_id,
                  created_by=created_by, seq=_next_seq(db, pkg.skill_id))
    db.commit()
    return row.version_id


def list_versions(db: Session, skill_id: str | None = None) -> list[SkillVersionRow]:
    q = db.query(EvolutionSkillVersion)
    if skill_id is not None:
        q = q.filter(EvolutionSkillVersion.skill_id == skill_id)
    rows = q.order_by(EvolutionSkillVersion.skill_id,
                      EvolutionSkillVersion.seq.asc()).all()
    return [SkillVersionRow.from_model(r) for r in rows]


def get_version(db: Session, version_id: str) -> SkillVersionRow:
    """精确取版本并做完整性校验（内容哈希失配 → 视为损坏，明确失败）。"""
    row = db.query(EvolutionSkillVersion).filter(
        EvolutionSkillVersion.version_id == version_id).first()
    if row is None:
        raise SkillStoreError(f"技能版本缺失: {version_id}")
    actual = _recompute_hash(
        row.version_id, row.skill_id, row.domain, row.runtime_ref,
        row.schema_version, row.skill_md or "", row.purpose_md or "")
    if actual != row.content_hash:
        raise SkillStoreError(
            f"技能版本损坏（内容哈希失配）: {version_id}（不可静默替换）")
    return SkillVersionRow.from_model(row)


# ---------------------------------------------------------------------------
# 绑定（experiment / business 分开；无行=关闭；empty 行=显式空技能集合）
# ---------------------------------------------------------------------------


def _scope_query(db: Session, *, kind: str, workspace_id: str, domain: str):
    return (
        db.query(EvolutionSkillBinding)
        .filter(EvolutionSkillBinding.kind == kind,
                EvolutionSkillBinding.workspace_id == workspace_id,
                EvolutionSkillBinding.domain == domain)
    )


def _binding_member_spec(ver: SkillVersionRow) -> dict:
    """单版本绑定成员规范（与 business binding_set_hash 契约一致：含 seq）。"""
    return {"skill_id": ver.skill_id, "version_id": ver.version_id,
            "content_hash": ver.content_hash, "seq": int(ver.seq)}


def _materialized_binding(db: Session, *, kind: str, workspace_id: str,
                          domain: str, set_kind: str, skill_id: str | None,
                          version_id: str | None,
                          members: list[dict]) -> None:
    """binding 写路径统一物化：members_json/set_hash/rev 与内容同写，绝不让
    version_id 单独变化而留下过期 members_json/set_hash/rev。

    集合哈希使用与 business_ops.binding_set_hash 相同的算法（惰性导入避免
    模块环）；新行 rev=1，切换行 rev+1。
    """
    from app.core.skill_evolution import business_ops as _bops  # 惰性：防循环
    import json as _json
    row = _scope_query(db, kind=kind, workspace_id=workspace_id,
                       domain=domain).first()
    if row is None:
        import uuid as _uuid
        row = EvolutionSkillBinding(
            id=str(_uuid.uuid4()), kind=kind, workspace_id=workspace_id,
            domain=domain, set_kind=set_kind, skill_id=skill_id,
            version_id=version_id, rev=1,
            set_hash=_bops.binding_set_hash(members),
            members_json=_json.dumps(members, ensure_ascii=False))
        db.add(row)
    else:
        row.set_kind = set_kind
        row.skill_id = skill_id
        row.version_id = version_id
        row.rev = int(row.rev or 1) + 1
        row.set_hash = _bops.binding_set_hash(members)
        row.members_json = _json.dumps(members, ensure_ascii=False)


def bind(db: Session, *, kind: str, workspace_id: str, domain: str,
         version_id: str | None = None, empty: bool = False) -> str:
    """写入/切换绑定（集合物化语义）。skill 形态校验版本存在且 domain/Runtime
    兼容；空集合显式记录（members_json='[]' + 空集合哈希）。"""
    if kind not in ("experiment", "business"):
        raise SkillStoreError(f"非法绑定 kind: {kind}")
    if domain != DEFAULT_DOMAIN:
        raise SkillStoreError(
            f"绑定暂不支持 domain={domain!r}（仅 {DEFAULT_DOMAIN!r}）")
    if empty:
        set_kind, skill_id, ver = SET_KIND_EMPTY, None, None
        members: list[dict] = []
    else:
        if not version_id:
            raise SkillStoreError("绑定技能形态需要 version_id")
        ver = get_version(db, version_id)  # 缺失/损坏 → 抛
        if ver.domain != domain or ver.runtime_ref != RUNTIME_REF:
            raise SkillStoreError(
                f"版本 {version_id} 与该 domain/Runtime 不兼容（拒绝绑定）")
        set_kind, skill_id, ver_id = SET_KIND_SKILL, ver.skill_id, ver.version_id
        members = [_binding_member_spec(ver)]
        ver = ver_id
    _materialized_binding(db, kind=kind, workspace_id=workspace_id,
                          domain=domain, set_kind=set_kind, skill_id=skill_id,
                          version_id=ver, members=members)
    row = _scope_query(db, kind=kind, workspace_id=workspace_id,
                       domain=domain).first()
    db.commit()
    return row.id


def unbind(db: Session, *, kind: str, workspace_id: str, domain: str) -> bool:
    row = _scope_query(db, kind=kind, workspace_id=workspace_id,
                       domain=domain).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def list_bindings(db: Session, *, kind: str | None = None) -> list[dict]:
    q = db.query(EvolutionSkillBinding)
    if kind is not None:
        q = q.filter(EvolutionSkillBinding.kind == kind)
    rows = q.order_by(EvolutionSkillBinding.kind,
                      EvolutionSkillBinding.workspace_id).all()
    return [
        {
            "kind": r.kind,
            "workspace_id": r.workspace_id,
            "domain": r.domain,
            "set_kind": r.set_kind,
            "skill_id": r.skill_id,
            "version_id": r.version_id,
        }
        for r in rows
    ]


def resolve_binding(db: Session, *, kind: str, workspace_id: str, domain: str) -> dict:
    """解析绑定：'none'（关闭）/'empty'（显式空集合）/ skill 版本信息。"""
    row = _scope_query(db, kind=kind, workspace_id=workspace_id,
                       domain=domain).first()
    if row is None:
        return {"mode": "none", "version": None}
    if row.set_kind == SET_KIND_EMPTY:
        return {"mode": "empty", "version": None}
    version = get_version(db, row.version_id)
    return {"mode": "skill", "version": version}


def freeze_versions(db: Session, version_ids,
                    expected_hashes=None) -> FrozenSkillSet:
    """按精确版本冻结技能集合；缺失/损坏/哈希与记录不符 → 明确失败。

    用于：run --skills-version / rerun（重放默认用原执行记录中的版本与内容哈希，
    绝不自动使用最新版本）。
    """
    rows = []
    for i, vid in enumerate(version_ids):
        row = get_version(db, vid)  # 缺失 / 内容哈希失配（损坏）→ SkillStoreError
        if expected_hashes is not None:
            if i >= len(expected_hashes) or expected_hashes[i] != row.content_hash:
                raise SkillStoreError(
                    f"技能版本 {vid} 内容哈希与原执行记录不一致"
                    f"（记录 {expected_hashes[i] if i < len(expected_hashes) else '-'} "
                    f"实际 {row.content_hash}），拒绝静默替换")
        rows.append(row)
    return FrozenSkillSet.from_versions(tuple(rows))
