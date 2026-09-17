"""阶段 8D：业务晋升与技能回退（business_ops）。

两套操作严格分开：
- 实验接受：gate 已接受的技能集合（experiment store 内 current/best 指针），
  由 orchestrator/门控在运行内自动/显式完成；本模块只读取其证据。
- 业务晋升：把“精确不可变版本”复制进业务库 evolution_skill_versions 并写入
  business 绑定（workspace + domain），业务编译请求据此读取作用域指令。

规则落实：
- 晋升前展示来源实验/精确版本/评估有效性/真实或模拟证据/目标作用域；
- 服务端默认阻止 模拟 / 未验证 / 评估无效 候选晋升（allow_simulated=True 是显式
  越权旁路，仍永不把模拟证据标为真实效果）；
- 管理员权限、作用域校验、幂等（idempotency_key 唯一 + 当前绑定同版本幂等）与
  当前绑定版本比较在服务层做；绑定更新与审计事件同事务原子；
- 回退到该作用域历史上“明确生效过”的兼容版本（按审计链回退），无历史 → 错误；
- 业务库缺 evolution schema → provisioned=False，晋升/回退明确报缺表（不自动迁移）；
- 内容哈希在复制前校验；损坏/记录缺失 → 明确失败，绝不写入不一致绑定。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import inspect as sa_inspect, text as sa_text
from sqlalchemy.exc import IntegrityError

from app.core.skill_evolution import skill_store
from app.core.skill_evolution.contracts import canonical_json
from app.core.skill_evolution.errors import SkillEvolutionError
from app.core.skill_evolution.injector import (
    FrozenSkillSet,
    SkillInjectingRunner,
    SkillSetError,
    render_execution_skill_text,
    set_hash_for_members,
)

logger = logging.getLogger(__name__)

DOMAIN = skill_store.DEFAULT_DOMAIN  # wiki_compile.default
BINDING_TABLE = "evolution_skill_bindings"
VERSION_TABLE = "evolution_skill_versions"
EVENT_TABLE = "evolution_business_events"
WORKSPACE_TABLE = "wiki_workspaces"

_EVOLUTION_SCHEMA_VERSION = "skill-evolution/v1"

# 完整集合契约（B2/B3）：
# - 成员上限：晋升目标集合最多 MAX_SET_MEMBERS 个成员（防御超长集合）；
# - 两种哈希算法刻意不同，绝不互比字符串，仅以明确 schema_version 区分并各自
#   用完整成员 + 精确内容哈希证明等价：
#   * 实验侧（experiment-set/v1）= injector.set_hash_for_members（成员仅
#     skill_id/version_id/content_hash，排序后哈希，空集 sha256('[]')）；
#   * 业务侧（business-binding-set/v1）= binding_set_hash（保序，含 seq，
#     空集 _hash({"members": []})）。
MAX_SET_MEMBERS = 8
EXPERIMENT_SET_SCHEMA_VERSION = "experiment-set/v1"
BINDING_SET_SCHEMA_VERSION = "business-binding-set/v1"

# 首次绑定（当前无绑定行）的显式预期令牌：expected_rev=0 且
# expected_set_hash=NO_BINDING_HASH。NO_BINDING_HASH 与空集合哈希
# (binding_set_hash([]))、空版本哈希刻意不同，绝不混淆“无绑定/空集合/旧行”。
FIRST_BIND_REV = 0


class BusinessOpsError(SkillEvolutionError):
    def __init__(self, message: str, code: str = "business_ops_error",
                 *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


def _hash(payload: dict) -> str:
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_payload(skill_id: str, domain: str, runtime_ref: str,
                       skill_md: str, purpose_md: str) -> dict:
    return {"skill_id": skill_id, "domain": domain, "runtime_ref": runtime_ref,
            "schema_version": _EVOLUTION_SCHEMA_VERSION,
            "skill_md": skill_md, "purpose_md": purpose_md}


def schema_provisioned(db) -> bool:
    insp = sa_inspect(db.get_bind())
    return all(insp.has_table(t)
               for t in (BINDING_TABLE, VERSION_TABLE, EVENT_TABLE))


def _require_provisioned(db) -> None:
    if not schema_provisioned(db):
        raise BusinessOpsError(
            "业务库缺少 WikiSkill schema（evolution_skill_* / "
            f"evolution_business_events 未迁移）：不自动迁移生产库；"
            "请先离线执行迁移 c8d9e0f1a2b3/HEAD 对应版本（P51）。",
            code="schema_not_provisioned", status_code=503)


# ---------------------------------------------------------------------------
# 业务库侧只读：绑定集合解析（编译桥接 / 状态查询 / B3 CAS 预期共用底座）
# ---------------------------------------------------------------------------

_P52_BINDING_COLS = frozenset({"rev", "set_hash", "members_json"})


def _binding_has_p52(db) -> bool:
    """evolution_skill_bindings 是否已具备 P52 集合列。

    P51（未执行 P52 迁移）生产库无这三列 → 返回 False，读取方一律按旧行语义
    （members_json 视为 NULL：旧单技能/旧空集合；rev=1、set_hash=None）。
    """
    try:
        names = {c["name"]
                 for c in sa_inspect(db.get_bind()).get_columns(BINDING_TABLE)}
    except Exception:  # noqa: BLE001
        return False
    return _P52_BINDING_COLS <= names


def _load_binding_member(db, *, skill_id: str, version_id: str,
                         workspace_id: str, domain: str,
                         claimed_hash: str | None = None,
                         claimed_seq: int | None = None) -> dict:
    """读取并全量校验单个绑定成员版本；返回成员规范字段 + 版本内容。

    - 版本行缺失 → binding_version_missing；
    - 版本行内容哈希失配（损坏）→ binding_version_corrupt；
    - 成员声明与版本记录不一致（skill_id / content_hash / seq）→
      binding_member_inconsistent；
    - 成员 domain/runtime 与本作用域不兼容 → binding_member_incompatible。
    """
    v = db.execute(sa_text(
        "SELECT version_id, skill_id, seq, domain, runtime_ref, schema_version, "
        "skill_md, purpose_md, content_hash FROM evolution_skill_versions "
        "WHERE version_id=:v"), {"v": version_id}).fetchone()
    if v is None:
        raise BusinessOpsError(
            f"业务绑定成员版本缺失: skill_id={skill_id} version_id={version_id}"
            f"（工作区 {workspace_id}）；任一成员缺失整体失败",
            code="binding_version_missing")
    expected = _hash(_canonical_payload(
        v.skill_id, v.domain, v.runtime_ref, v.skill_md, v.purpose_md))
    if expected != v.content_hash:
        raise BusinessOpsError(
            f"业务绑定成员版本内容损坏（哈希失配）: {version_id}（工作区 "
            f"{workspace_id}）；不静默跳过或只保留首成员",
            code="binding_version_corrupt")
    if v.skill_id != skill_id:
        raise BusinessOpsError(
            f"绑定成员 skill_id 与版本记录不一致: 绑定 {skill_id} / 记录 "
            f"{v.skill_id}（version_id={version_id}，工作区 {workspace_id}）",
            code="binding_member_inconsistent")
    if claimed_hash is not None and claimed_hash != v.content_hash:
        raise BusinessOpsError(
            f"绑定成员声明的 content_hash 与版本记录不一致: {version_id}（工作区 "
            f"{workspace_id}）；拒绝不一致集合",
            code="binding_member_inconsistent")
    if claimed_seq is not None and int(claimed_seq) != int(v.seq):
        raise BusinessOpsError(
            f"绑定成员声明的 seq 与版本记录不一致: {version_id} 记录 seq="
            f"{v.seq}，成员声明 seq={claimed_seq}（工作区 {workspace_id}）",
            code="binding_member_inconsistent")
    if v.domain != domain or v.runtime_ref != skill_store.RUNTIME_REF:
        raise BusinessOpsError(
            f"业务绑定成员版本不兼容本作用域 domain/runtime: {version_id} "
            f"(domain={v.domain}, runtime_ref={v.runtime_ref})（工作区 "
            f"{workspace_id}）", code="binding_member_incompatible")
    return {"skill_id": v.skill_id, "version_id": v.version_id,
            "seq": int(v.seq), "content_hash": v.content_hash,
            "domain": v.domain, "runtime_ref": v.runtime_ref,
            "schema_version": v.schema_version,
            "skill_md": v.skill_md, "purpose_md": v.purpose_md}


def resolve_business_binding_set(db, workspace_id: str,
                                 domain: str = DOMAIN, *,
                                 strict_schema: bool = False) -> dict | None:
    """统一读取业务绑定的完整技能集合（B2 只读解析）。

    无绑定行 → None（旧行为：不注入）。否则返回：
        {"rev", "kind", "members", "set_hash", "canonical_set_hash", "legacy"}
      - rev:              绑定修订号（旧行 / P51 未迁移库按 1）；
      - kind:             'skill' | 'empty'（空集合与旧单技能由 kind/members
                          明确区分，绝不混淆）；
      - members:          按绑定固定顺序展开的完整成员（规范字段 + 版本内容）；
                          空集合为 []；
      - set_hash:         行内存储的规范集合哈希（旧行可为 None）；
      - canonical_set_hash: 读时按完整成员重算的集合哈希（binding_set_hash）；
      - legacy:           members_json IS NULL 的旧记录（旧单技能 / 旧空集合）。

    members_json IS NULL → 按旧单技能记录（set_kind='skill' + skill_id/
    version_id）或旧空集合（set_kind='empty'）兼容读取（legacy=True）。

    全量校验（任一失败整体抛 BusinessOpsError，不静默跳过、不保留首成员）：
      版本行缺失 / 内容哈希失配、成员声明与版本记录不一致（skill_id/
      content_hash/seq）、domain/runtime 不兼容、成员重复、存储顺序非规范
      （未按 (skill_id, seq) 严格升序）、集合哈希不一致。
    """
    if not schema_provisioned(db):
        if strict_schema:
            raise BusinessOpsError(
                "业务库缺少 WikiSkill schema（evolution_skill_* 未迁移）："
                "功能已启用但 schema 缺失——拒绝继续（不静默忽略绑定）；"
                "请先离线执行迁移（P51 对应版本）。",
                code="schema_not_provisioned", status_code=503)
        return None
    if _binding_has_p52(db):
        row = db.execute(sa_text(
            "SELECT set_kind, skill_id, version_id, rev, set_hash, members_json "
            "FROM evolution_skill_bindings "
            "WHERE kind='business' AND workspace_id=:w AND domain=:d"),
            {"w": workspace_id, "d": domain}).fetchone()
        if row is None:
            return None
        rev = int(row.rev or 1)
        stored_hash = row.set_hash
        members_raw = row.members_json
        legacy = members_raw is None
        set_kind = row.set_kind
    else:
        # P51 未迁移库：无 P52 列，一律按旧行语义读取。
        row = db.execute(sa_text(
            "SELECT set_kind, skill_id, version_id FROM evolution_skill_bindings "
            "WHERE kind='business' AND workspace_id=:w AND domain=:d"),
            {"w": workspace_id, "d": domain}).fetchone()
        if row is None:
            return None
        rev = 1
        stored_hash = None
        members_raw = None
        legacy = True
        set_kind = row.set_kind
    if set_kind not in ("skill", "empty"):
        raise BusinessOpsError(
            f"非法绑定 set_kind: {set_kind!r}（工作区 {workspace_id}）",
            code="binding_members_corrupt")
    # --- 成员规范：旧行按 skill_id/version_id 兼容合成；新行解析 members_json ---
    specs: list[dict] = []
    if legacy:
        if set_kind == "skill":
            if not (row.skill_id and row.version_id):
                raise BusinessOpsError(
                    f"旧单技能绑定行缺 skill_id/version_id 配对（工作区 "
                    f"{workspace_id}）", code="binding_members_corrupt")
            specs = [{"skill_id": row.skill_id, "version_id": row.version_id,
                      "content_hash": None, "seq": None}]
    else:
        try:
            raw_members = json.loads(members_raw)
        except (ValueError, TypeError):
            raise BusinessOpsError(
                f"业务绑定 members_json 损坏（非合法 JSON，工作区 "
                f"{workspace_id}）", code="binding_members_corrupt")
        if not isinstance(raw_members, list):
            raise BusinessOpsError(
                f"业务绑定 members_json 非成员列表（工作区 {workspace_id}）",
                code="binding_members_corrupt")
        seen_skills: set[str] = set()
        seen_versions: set[str] = set()
        for i, m in enumerate(raw_members):
            if not isinstance(m, dict):
                raise BusinessOpsError(
                    f"业务绑定 members_json 第 {i} 项非对象（工作区 "
                    f"{workspace_id}）", code="binding_members_corrupt")
            skill_id = m.get("skill_id")
            version_id = m.get("version_id")
            content_hash = m.get("content_hash")
            seq = m.get("seq")
            if (not isinstance(skill_id, str) or not skill_id
                    or not isinstance(version_id, str) or not version_id
                    or not isinstance(content_hash, str) or not content_hash):
                raise BusinessOpsError(
                    f"业务绑定 members_json 第 {i} 项缺少规范字段 "
                    "(skill_id/version_id/content_hash，工作区 "
                    f"{workspace_id})", code="binding_members_corrupt")
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise BusinessOpsError(
                    f"业务绑定 members_json 第 {i} 项 seq 非整数（工作区 "
                    f"{workspace_id}）", code="binding_members_corrupt")
            if skill_id in seen_skills:
                raise BusinessOpsError(
                    f"业务绑定成员重复（同一 skill_id 出现多次）: {skill_id} "
                    f"（工作区 {workspace_id}）；每技能最多一个成员",
                    code="binding_member_duplicate")
            if version_id in seen_versions:
                raise BusinessOpsError(
                    f"业务绑定成员重复（同一 version_id 出现多次）: {version_id}"
                    f"（工作区 {workspace_id}）",
                    code="binding_member_duplicate")
            seen_skills.add(skill_id)
            seen_versions.add(version_id)
            specs.append({"skill_id": skill_id, "version_id": version_id,
                          "content_hash": content_hash, "seq": seq})
    members = [_load_binding_member(
        db, skill_id=s["skill_id"], version_id=s["version_id"],
        workspace_id=workspace_id, domain=domain,
        claimed_hash=s["content_hash"], claimed_seq=s["seq"])
        for s in specs]
    if bool(members) != (set_kind == "skill"):
        raise BusinessOpsError(
            f"绑定 set_kind 与成员集合不一致: kind={set_kind!r} 成员数="
            f"{len(members)}（工作区 {workspace_id}）",
            code="binding_set_kind_mismatch")
    # 顺序：成员须按 (skill_id, seq) 严格升序（重复 skill_id/seq 已被上式拒绝）。
    if len(members) > 1:
        prev = (members[0]["skill_id"], members[0]["seq"])
        for m in members[1:]:
            cur = (m["skill_id"], m["seq"])
            if cur <= prev:
                raise BusinessOpsError(
                    "业务绑定成员顺序非规范（须按 skill_id/seq 严格升序）: "
                    + ", ".join(f"{x['skill_id']}#{x['version_id']}"
                                for x in members)
                    + f"（工作区 {workspace_id}）",
                    code="binding_member_order")
            prev = cur
    canonical = binding_set_hash(members)
    if stored_hash is not None and stored_hash != canonical:
        raise BusinessOpsError(
            "业务绑定存储集合哈希与完整成员重算不一致（内容/顺序被改动或写坏）: "
            f"存储 {stored_hash}，重算 {canonical}（工作区 {workspace_id}）",
            code="binding_set_hash_mismatch")
    if not legacy and stored_hash is None:
        raise BusinessOpsError(
            f"新格式绑定缺少物化 set_hash（members_json 非 NULL，工作区 "
            f"{workspace_id}）", code="binding_set_hash_mismatch")
    return {"rev": rev, "kind": set_kind, "members": members,
            "set_hash": stored_hash, "canonical_set_hash": canonical,
            "legacy": legacy}


def resolve_business_binding(db, workspace_id: str,
                             domain: str = DOMAIN, *,
                             strict_schema: bool = False) -> dict | None:
    """返回当前 business 绑定的技能内容（单技能调用方兼容接口）。

    - 无绑定 / 空集合 → None（旧行为不变，不注入）；
    - 旧单技能行（members_json IS NULL）与新格式单成员集合 → 原单版本字典；
    - 多成员集合 → BusinessOpsError(binding_set_multi_member)：明确失败，绝不
      把多成员集合静默压成首成员（多成员注入由后续冻结/渲染集合化改造接入）；
    - 缺 schema：strict_schema=False（状态查询等）→ None；
      strict_schema=True（功能明确启用时的编译路径）→ 抛 schema_not_provisioned；
    - 任一成员版本缺失/损坏/集合不一致/哈希失配 → BusinessOpsError（fail loud）。
    """
    st = resolve_business_binding_set(db, workspace_id, domain=domain,
                                      strict_schema=strict_schema)
    if st is None or st["kind"] == "empty":
        return None
    if len(st["members"]) > 1:
        raise BusinessOpsError(
            f"当前绑定为多成员技能集合（{len(st['members'])} 成员，工作区 "
            f"{workspace_id}）：单技能读取接口不适用，不得静默取首成员",
            code="binding_set_multi_member")
    m = st["members"][0]
    return {"version_id": m["version_id"], "skill_id": m["skill_id"],
            "skill_md": m["skill_md"], "purpose_md": m["purpose_md"],
            "content_hash": m["content_hash"]}


def render_instruction_block(binding: dict) -> str:
    """（兼容单技能调用方）把单个绑定的技能指令渲染为执行正文。

    与实验侧 FrozenSkillSet 使用同一纯函数；PURPOSE.md 不进入执行提示词。
    """
    try:
        return render_execution_skill_text([binding])
    except SkillSetError as exc:
        raise BusinessOpsError(str(exc), code="binding_set_too_large") from exc


def render_set_block(members: list[dict]) -> str:
    """完整业务绑定集合的执行正文（与实验注入同一函数，仅 SKILL.md）。"""
    try:
        return render_execution_skill_text(members)
    except SkillSetError as exc:
        raise BusinessOpsError(str(exc), code="binding_set_too_large") from exc


def domain_for_pipeline(pipeline_key: str) -> str | None:
    """编译流水线 → WikiSkill 作用域 domain 映射（仅 wiki.default 相关）。"""
    key = str(pipeline_key or "")
    if key in ("wiki.default", "wiki_compile.default"):
        return DOMAIN
    return None


ARTIFACT_TYPE_EVOLUTION_PIN = "evolution_binding_pin"
PIN_SCHEMA_VERSION = "evolution-binding-pin/v1"


def _load_pin(db, run_id: str) -> dict | None:
    rows = db.execute(sa_text(
        "SELECT payload_json FROM knowledge_compile_artifacts "
        "WHERE run_id=:rid AND artifact_type=:t "
        "ORDER BY created_at, id LIMIT 1"),
        {"rid": run_id, "t": ARTIFACT_TYPE_EVOLUTION_PIN}).fetchall()
    if not rows or rows[0].payload_json is None:
        return None
    try:
        payload = json.loads(rows[0].payload_json)
    except (ValueError, TypeError):
        raise BusinessOpsError(
            f"编译 run 的技能版本冻结记录损坏: {run_id}",
            code="binding_pin_corrupt")
    return payload


def _record_pin(db, run_id: str, payload: dict) -> None:
    from app.models.database import KnowledgeCompileArtifact as Artifact
    import uuid as _uuid
    row = Artifact(
        id="pin_" + _uuid.uuid4().hex[:20], run_id=run_id,
        artifact_type=ARTIFACT_TYPE_EVOLUTION_PIN,
        schema_version=PIN_SCHEMA_VERSION,
        object_type="evolution_skill_binding",
        payload_json=json.dumps(payload, ensure_ascii=False, default=str))
    db.add(row)
    db.commit()


def _pin_members(payload: dict) -> list[dict]:
    """从 pin 解析目标成员规范（含版本内容装载所需键）。

    新 pin：payload["members"] = [{skill_id,version_id,content_hash,seq}...]；
    旧单技能 pin（无 members，仅 version_id/skill_id/content_hash）→ 兼容为
    单成员规范（seq 未知，装载时按版本行确认），不改变其原含义。
    """
    members = payload.get("members")
    if members is not None:
        if not isinstance(members, list):
            raise BusinessOpsError("冻结 pin 的 members 非列表",
                                   code="binding_pin_corrupt")
        return [{"skill_id": m["skill_id"], "version_id": m["version_id"],
                 "content_hash": m["content_hash"], "seq": m["seq"]}
                for m in members]
    vid = payload.get("version_id")
    if vid:
        return [{"skill_id": payload.get("skill_id"),
                 "version_id": vid,
                 "content_hash": payload.get("content_hash"), "seq": None}]
    return []


def _apply_pin(ctx: dict, db, payload: dict, domain: str) -> None:
    """按已冻结 pin 包装 llm_runner；绑定中途变更不影响该 run。

    - mode=none：run 开始即无绑定 → 全程不注入；
    - mode=bound：每次 attempt 重读全部成员版本行并逐成员校验（不可变；任一
      缺失/损坏/与冻结声明不一致 → fail loud，绝不忽略或只校验首成员）。
    """
    if payload.get("mode") != "bound":
        return
    specs = _pin_members(payload)
    full = []
    for spec in specs:
        v = db.execute(sa_text(
            "SELECT version_id, skill_id, seq, domain, runtime_ref, "
            "schema_version, skill_md, purpose_md, content_hash FROM "
            "evolution_skill_versions WHERE version_id=:v"),
            {"v": spec["version_id"]}).fetchone()
        if v is None:
            raise BusinessOpsError(
                f"编译 run 冻结的技能版本缺失: {spec['version_id']}（该 run 中途"
                "绑定变更不允许改变固定集合；版本被移除属异常）",
                code="pinned_version_missing")
        expected = _hash(_canonical_payload(
            v.skill_id, v.domain, v.runtime_ref, v.skill_md, v.purpose_md))
        if expected != v.content_hash:
            raise BusinessOpsError(
                f"编译 run 冻结的技能版本内容损坏: {v.version_id}",
                code="pinned_version_corrupt")
        if spec.get("skill_id") is not None and v.skill_id != spec["skill_id"]:
            raise BusinessOpsError(
                f"编译 run 冻结成员 skill_id 与版本记录不一致: {v.version_id}",
                code="pinned_version_corrupt")
        if spec.get("content_hash") is not None and \
                v.content_hash != spec["content_hash"]:
            raise BusinessOpsError(
                f"编译 run 冻结成员 content_hash 与版本记录不一致: {v.version_id}",
                code="pinned_version_corrupt")
        if spec.get("seq") is not None and int(spec["seq"]) != int(v.seq):
            raise BusinessOpsError(
                f"编译 run 冻结成员 seq 与版本记录不一致: {v.version_id}",
                code="pinned_version_corrupt")
        full.append({"skill_id": v.skill_id, "version_id": v.version_id,
                     "content_hash": v.content_hash, "seq": int(v.seq),
                     "skill_md": v.skill_md, "purpose_md": v.purpose_md})
    if not full:
        raise BusinessOpsError(
            "编译 run 冻结的绑定集合为空（bound pin 无成员）",
            code="binding_pin_corrupt")
    try:
        plan = FrozenSkillSet.from_versions(_pin_version_rows(full))
    except SkillSetError as exc:
        raise BusinessOpsError(str(exc), code="binding_set_too_large") from exc
    ctx["llm_runner"] = _binding_injecting_runner(ctx.get("llm_runner"), plan)
    set_hash = payload.get("set_hash") or binding_set_hash(_spec_list(full))
    ctx["evolution_binding"] = {
        "members": _spec_list(full),
        "set_hash": set_hash,
        "domain": domain,
        "pinned": True,
        "note": "已注入本 run 冻结时的业务绑定集合（中途晋升/回退不影响本 run）",
    }


def freeze_binding_for_run(ctx: dict, db, run_id: str,
                           workspace_id: str | None,
                           domain: str = DOMAIN) -> None:
    """编译 run 首次执行时冻结完整业务绑定集合并落盘；恢复沿用冻结值。

    - 功能开关关闭 → 完全不动（旧行为不变，也不写 pin）；
    - 无既有 pin → 现场解析完整集合（schema 缺失且开关开 → fail loud）并落盘；
    - 同一 run 中断恢复 → 读 pin：中途晋升/回退不能改变该 run 的冻结集合。
    """
    from app.config import settings
    if not settings.wikiskill_business_compile_enabled:
        return
    if not workspace_id:
        return
    existing = _load_pin(db, run_id)
    if existing is not None:
        _apply_pin(ctx, db, existing, domain)
        return
    binding = resolve_business_binding_set(db, workspace_id, domain,
                                           strict_schema=True)
    if binding is None or binding["kind"] == "empty":
        _record_pin(db, run_id, {"mode": "none", "domain": domain,
                                 "pinned_at": datetime.now().isoformat()})
        return
    payload = {"mode": "bound", "domain": domain,
               "members": _spec_list(binding["members"]),
               "set_hash": binding["canonical_set_hash"],
               "version_id": binding["members"][0]["version_id"],
               "skill_id": binding["members"][0]["skill_id"],
               "content_hash": binding["members"][0]["content_hash"],
               "pinned_at": datetime.now().isoformat()}
    _record_pin(db, run_id, payload)
    _apply_pin(ctx, db, payload, domain)


def _pin_version_rows(members: list[dict]):
    from types import SimpleNamespace
    return [SimpleNamespace(
        skill_id=m["skill_id"], version_id=m["version_id"],
        content_hash=m["content_hash"], seq=int(m.get("seq") or 0),
        skill_md=m.get("skill_md") or "") for m in members]


def _binding_injecting_runner(runner, plan: FrozenSkillSet):
    """业务编译注入：复用实验侧 SkillInjectingRunner + FrozenSkillSet。"""
    inner = runner
    if inner is None:
        from app.core.wiki_pipeline.pipelines.wiki_default import (
            _default_llm_runner)
        inner = _default_llm_runner

    def _noop_emit(_event: dict) -> None:
        return None

    return SkillInjectingRunner(plan, inner, _noop_emit)


def _binding_error_runner(runner, exc: BusinessOpsError):
    def _raise_binding(messages, context="", timeout=120.0):
        raise BusinessOpsError(str(exc.args[0] if exc.args else exc),
                               code=getattr(exc, "code", "binding_error"))

    return _raise_binding


# executor 等调用方使用的公开别名。
binding_error_runner = _binding_error_runner


# ---------------------------------------------------------------------------
# 实验侧证据
# ---------------------------------------------------------------------------


def _accepted_set(exp_row) -> dict | None:
    """门控接受的当前候选集合文档（best/current 整组，非首成员）。

    解析 best/current_skill_set_json（FrozenSkillSet.to_dict() 形态：mode +
    members[{skill_id, version_id, content_hash, seq}] + set_hash）。
    优先 best；best 为空集合/缺失时回退 current；两者皆空 → None。
    """
    for key in ("best_skill_set_json", "current_skill_set_json"):
        raw = getattr(exp_row, key, None) or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and (data or {}).get("members"):
            return data
    return None


def canonical_binding_members(exp_members: list[dict]) -> list[dict]:
    """实验集合成员声明 → 业务集合规范成员（唯一转换入口；纯函数）。

    顺序契约：业务绑定固定顺序 == 实验实际注入顺序 == (skill_id, seq) 升序
    （injector.FrozenSkillSet.from_versions 对版本行按 _member_key=(skill_id,
    seq) 排序后逐成员输出；实验 side 评估/注入都经 from_versions，见 gating
    build_set_from_members）。

    校验（任一失败抛 BusinessOpsError，不静默跳过/不保留首成员）：
      - 每成员 skill_id/version_id/content_hash 非空、seq 为整数；
      - skill_id / version_id 不重复（每技能最多一个版本）；
      - 成员总数 ≤ MAX_SET_MEMBERS。
    返回规范四键成员列表（同一顺序）。
    """
    if len(exp_members) > MAX_SET_MEMBERS:
        raise BusinessOpsError(
            f"晋升目标集合成员超限: {len(exp_members)} > {MAX_SET_MEMBERS} "
            "（拒绝写入）", code="exp_set_invalid")
    seen_skills: set[str] = set()
    seen_versions: set[str] = set()
    for i, m in enumerate(exp_members):
        if not isinstance(m, dict):
            raise BusinessOpsError(
                f"实验接受集合第 {i} 项非对象", code="exp_set_invalid")
        skill_id = m.get("skill_id")
        version_id = m.get("version_id")
        content_hash = m.get("content_hash")
        seq = m.get("seq")
        if (not isinstance(skill_id, str) or not skill_id
                or not isinstance(version_id, str) or not version_id
                or not isinstance(content_hash, str) or not content_hash):
            raise BusinessOpsError(
                f"实验接受集合第 {i} 项缺少 skill_id/version_id/content_hash "
                "（记录损坏）", code="exp_set_invalid")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise BusinessOpsError(
                f"实验接受集合第 {i} 项 seq 非整数: {skill_id}",
                code="exp_set_invalid")
        if skill_id in seen_skills:
            raise BusinessOpsError(
                f"实验接受集合重复 skill_id（每技能最多一个版本）: {skill_id}",
                code="exp_set_invalid")
        if version_id in seen_versions:
            raise BusinessOpsError(
                f"实验接受集合重复 version_id: {version_id}",
                code="exp_set_invalid")
        seen_skills.add(skill_id)
        seen_versions.add(version_id)
    ordered = sorted(
        exp_members,
        key=lambda m: (str(m["skill_id"]), int(m["seq"])))
    return [{"skill_id": m["skill_id"], "version_id": m["version_id"],
             "content_hash": m["content_hash"], "seq": int(m["seq"])}
            for m in ordered]


def _exp_set_hash_of(exp_doc: dict) -> str:
    """实验来源集合哈希（experiment-set/v1 算法）。

    以 injector.set_hash_for_members 对完整成员重算；doc 自带 set_hash 时
    必须一致（同算法可比较；绝不与业务侧 binding_set_hash 互比字符串）。
    """
    claims = [{k: m[k] for k in ("skill_id", "version_id", "content_hash")}
              for m in (exp_doc.get("members") or [])]
    recomputed = set_hash_for_members(claims)
    stored = exp_doc.get("set_hash")
    if stored is not None and stored != recomputed:
        raise BusinessOpsError(
            "实验集合文档 set_hash 与按完整成员重算不一致（实验记录损坏）：拒绝"
            "晋升", code="exp_set_invalid")
    return recomputed


def evidence_for_experiment(db_exp, exp_row) -> dict:
    """实验侧证据摘要（模拟/真实按 run 配置如实标记，永不冒充真实调用）。"""
    exp_id = exp_row.experiment_id
    rows = db_exp.execute(sa_text(
        "SELECT run_id, status, stop_reason, config_json, used_model_calls, "
        "created_at FROM evolution_runs WHERE experiment_id=:e "
        "ORDER BY created_at DESC LIMIT 20"), {"e": exp_id}).fetchall()
    runs = []
    for r in rows:
        try:
            cfg = json.loads(r.config_json or "{}")
        except (ValueError, TypeError):
            cfg = {}
        mode = (cfg.get("runner") or {}).get("mode") or "simulated"
        real = bool((cfg.get("runner") or {}).get("real"))
        created_at = None
        if getattr(r, "created_at", None) is not None:
            try:
                created_at = r.created_at.isoformat()
            except AttributeError:
                created_at = str(r.created_at)
        runs.append({
            "run_id": r.run_id, "status": r.status,
            "stop_reason": r.stop_reason,
            "model_mode": mode, "real": real,
            "used_model_calls": r.used_model_calls,
            "created_at": created_at,
        })
    ev = db_exp.execute(sa_text(
        "SELECT evaluation_id, kind, valid, main_passed, main_total, "
        "invalid_reason FROM evolution_evaluations WHERE experiment_id=:e "
        "ORDER BY created_at DESC LIMIT 5"), {"e": exp_id}).fetchall()
    evals = [{"evaluation_id": x.evaluation_id, "kind": x.kind,
              "valid": bool(x.valid), "score": f"{x.main_passed}/{x.main_total}"
              if x.valid else None,
              "invalid_reason": x.invalid_reason} for x in ev]
    best = None
    if getattr(exp_row, "best_score_total", None) is not None:
        best = {"passed": exp_row.best_score_passed,
                "total": exp_row.best_score_total}
    return {"runs": runs, "evaluations": evals, "best_score": best,
            "grader_version": getattr(exp_row, "grader_version", None),
            "status": getattr(exp_row, "status", None)}


def promotion_env() -> str:
    """当前业务晋升环境（settings.wikiskill_promotion_env）。"""
    from app.config import settings
    env = str(getattr(settings, "wikiskill_promotion_env", "") or "").strip()
    return env or "production"


def _real_run_evidence(db_exp, exp_row) -> bool:
    """真实执行证据：该实验存在 配置 real=true 且已完成、有模型调用、
    数据集版本一致的 run。配置 real 本身不是证据；空/失败/无关 run 不算。"""
    exp_id = exp_row.experiment_id
    rows = db_exp.execute(sa_text(
        "SELECT config_json, status, stop_reason, used_model_calls, "
        "dataset_version FROM evolution_runs WHERE experiment_id=:e"),
        {"e": exp_id}).fetchall()
    for r in rows:
        try:
            cfg = json.loads(r.config_json or "{}")
        except (ValueError, TypeError):
            continue
        runner = cfg.get("runner") or {}
        if not bool(runner.get("real")):
            continue
        if r.status != "completed":
            continue
        if int(r.used_model_calls or 0) <= 0:
            continue
        if str(r.dataset_version or "") != str(exp_row.dataset_version or ""):
            continue
        return True
    return False


def _gate_anchor_matches(db_exp, exp_row, members: list[dict]) -> bool:
    """受控记录锚点：存在 accepted 门控事件，其 next_set 完整成员 == 晋升目标
    （精确集合绑定；同集合不同事件只认内容一致）。"""
    exp_id = exp_row.experiment_id
    target = _spec_list(members)
    rows = db_exp.execute(sa_text(
        "SELECT next_set_json FROM evolution_gate_events "
        "WHERE experiment_id=:e AND decision='accepted'"),
        {"e": exp_id}).fetchall()
    for r in rows:
        try:
            data = json.loads(r.next_set_json or "{}")
        except (ValueError, TypeError):
            continue
        next_members = (data or {}).get("members") or []
        try:
            if _spec_list(next_members) == target:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _eligibility_report(db_exp, exp_row, accepted: dict) -> dict:
    """晋升资格报告（预览与执行共用同一判定，杜绝“预览可过、执行可绕”）。

    - 生产环境（promotion_env == production）：本包不开放生产晋升资格
      （真实供应商执行/人工校准批准为独立待办）——即使误设模拟旁路、请求
      allow_simulated=true、甚至存在 run 配置 real=true 也一律拒绝；
    - isolated-test：真实证据（completed+real+有调用+数据集一致）需命中受控
      门控接受记录（精确集合）；否则视为模拟证据，必须显式 allow_simulated
      且服务端开启隔离旁路（永不标为真实效果）。
    返回 {promotable, blocked, model_mode_evidence, real_evidence,
          gate_anchor_ok}；调用方把 blocked 并入预览/执行阻止。
    """
    from app.config import settings
    env = promotion_env()
    members = (accepted or {}).get("members") or []
    blocks: list[str] = []
    if env == "production":
        blocks.append(
            "生产环境不开放业务晋升（真实供应商执行、人工校准批准独立待办；"
            "配置 real 与模拟旁路开关均不构成生产资格）")
        return {"promotable": False, "blocked": blocks,
                "model_mode_evidence": "simulated",
                "real_evidence": False, "gate_anchor_ok": False}
    if not members:
        blocks.append("实验无可晋升的已接受集合（集合为空不晋升到业务）")
        return {"promotable": False, "blocked": blocks,
                "model_mode_evidence": "simulated",
                "real_evidence": False, "gate_anchor_ok": False}
    real_evidence = _real_run_evidence(db_exp, exp_row)
    anchor_ok = True
    if real_evidence:
        # 真实证据必须命中受控接受记录（精确集合），并仅在隔离测试环境允许。
        anchor_ok = _gate_anchor_matches(db_exp, exp_row, members)
        if not anchor_ok:
            blocks.append(
                "真实执行证据未命中受控门控接受记录（精确集合不一致/缺失）："
                "拒绝晋升")
    mode = "real" if real_evidence else "simulated"
    if not real_evidence:
        sim_allowed = bool(
            settings.wikiskill_evolution_allow_simulated_promotion)
        if not sim_allowed:
            blocks.append("证据来自模拟模式且服务端未开启隔离测试旁路：拒绝晋升")
    return {"promotable": not blocks, "blocked": blocks,
            "model_mode_evidence": mode, "real_evidence": real_evidence,
            "gate_anchor_ok": anchor_ok}


def _simulation_bypass_blocks(report: dict, allow_simulated: bool) -> list[str]:
    """请求级模拟旁路校验（仅 isolated-test 且服务端开启时可用）。"""
    from app.config import settings
    if report["model_mode_evidence"] == "real":
        return []
    server_side_allowed = bool(
        settings.wikiskill_evolution_allow_simulated_promotion)
    blocks = []
    if not allow_simulated:
        blocks.append("证据来自模拟模式（默认阻止业务晋升）；模拟证据不构成真实"
                      "效果，需显式 allow_simulated（仅隔离测试）")
    elif not server_side_allowed:
        blocks.append("模拟晋升旁路未在服务端开启（仅隔离测试环境可用；请求参数"
                      "不能开启）")
    return blocks


def promotion_preview(db_exp, exp_row, *, workspace_id: str,
                      allow_simulated: bool = False) -> dict:
    """晋升前展示（只读；不写库）。blocked 列表 = 阻止项（默认阻止即不晋升）。

    - target = 完整集合（非首成员）：members/exp_set_hash/evidence 指纹；
      target_version_id/content_hash 兼容保留（规范顺序首成员）；
    - 资格 = _eligibility_report（生产环境关闭 / isolated 受控记录锚定）+
      请求级模拟旁路校验；promotable 为最终结论；
    - preview_fingerprint 绑定本次预览的精确目标与证据，供提交端原样回传。
    """
    from app.config import settings  # noqa: F401（report 内使用）
    accepted = _accepted_set(exp_row)
    members = (accepted or {}).get("members") or []
    target_version = members[0] if members else None
    evidence = evidence_for_experiment(db_exp, exp_row)
    blocks: list[str] = []
    if accepted is None:
        blocks.append("实验无可晋升的已接受版本（current/best 集合为空）")
    elif not members:
        blocks.append("实验已接受集合为空（显式空集合不晋升到业务绑定）")
    if not (evidence.get("best_score") and evidence["best_score"]["total"]):
        blocks.append("实验没有有效的最优评估（评估无效/缺失）")
    valid_evals = [e for e in evidence["evaluations"] if e["valid"]]
    if not valid_evals and evidence["evaluations"]:
        blocks.append("最近评估记录均无效（invalid）")
    report = _eligibility_report(db_exp, exp_row, accepted) \
        if accepted is not None else {
            "promotable": False, "blocked": [],
            "model_mode_evidence": "simulated",
            "real_evidence": False, "gate_anchor_ok": False}
    blocks.extend(report["blocked"])
    blocks.extend(_simulation_bypass_blocks(report, allow_simulated))
    exp_hash = None
    if accepted is not None:
        try:
            exp_hash = _exp_set_hash_of(accepted)
        except BusinessOpsError:
            blocks.append("实验集合文档哈希与成员不一致（记录损坏）")
    fp_payload = {
        "experiment": exp_row.experiment_id,
        "target_members": _spec_list(members),
        "exp_set_hash": exp_hash,
        "real_evidence": report["real_evidence"],
        "gate_anchor_ok": report["gate_anchor_ok"],
        "model_mode_evidence": report["model_mode_evidence"],
    }
    preview_fingerprint = _hash(fp_payload)
    return {
        "experiment_id": exp_row.experiment_id,
        "workspace_id": workspace_id,
        "target_version_id": target_version["version_id"]
        if target_version else None,
        "target_content_hash": target_version.get("content_hash")
        if target_version else None,
        "members": [dict(m) for m in members],
        "exp_set_hash": exp_hash,
        "evidence": evidence,
        "model_mode_evidence": report["model_mode_evidence"],
        "real_evidence": report["real_evidence"],
        "gate_anchor_ok": report["gate_anchor_ok"],
        "real_link_verified": bool(report["real_evidence"]),
        "effect_verified": False,
        "blocked": blocks,
        "promotable": not blocks,
        "preview_fingerprint": preview_fingerprint,
    }


# ---------------------------------------------------------------------------
# 晋升 / 回退（业务库 + 审计，单事务）
# ---------------------------------------------------------------------------


def _copy_version_into_business(db, exp_row_row: dict, created_by: str) -> None:
    vid = exp_row_row["version_id"]
    exist = db.execute(sa_text(
        "SELECT content_hash FROM evolution_skill_versions WHERE version_id=:v"),
        {"v": vid}).fetchone()
    if exist is not None:
        if exist.content_hash != exp_row_row["content_hash"]:
            raise BusinessOpsError(
                f"业务库已存在同版本不同内容: {vid}（拒绝不一致覆盖）",
                code="version_conflict", status_code=422)
        return  # 幂等：同版本同内容已复制
    db.execute(sa_text(
        "INSERT INTO evolution_skill_versions (version_id, skill_id, seq, "
        "schema_version, domain, runtime_ref, parent_version_id, skill_md, "
        "purpose_md, content_hash, source_type, created_by, created_at) "
        "VALUES (:version_id,:skill_id,:seq,:schema_version,:domain,"
        ":runtime_ref,:parent_version_id,:skill_md,:purpose_md,:content_hash,"
        ":source_type,:created_by,:created_at)"),
        {"version_id": exp_row_row["version_id"],
         "skill_id": exp_row_row["skill_id"],
         "seq": exp_row_row["seq"],
         "schema_version": _EVOLUTION_SCHEMA_VERSION,
         "domain": exp_row_row["domain"],
         "runtime_ref": exp_row_row["runtime_ref"],
         "parent_version_id": exp_row_row.get("parent_version_id"),
         "skill_md": exp_row_row["skill_md"],
         "purpose_md": exp_row_row["purpose_md"],
         "content_hash": exp_row_row["content_hash"],
         "source_type": exp_row_row["source_type"],
         "created_by": created_by,
         "created_at": datetime.now()})


def _require_active_workspace(db, workspace_id: str) -> None:
    ws = db.execute(sa_text(
        "SELECT id FROM wiki_workspaces WHERE id=:w AND status='active'"),
        {"w": workspace_id}).fetchone()
    if ws is None:
        raise BusinessOpsError(f"目标作用域不存在或未激活: {workspace_id}",
                               code="workspace_invalid", status_code=422)


def binding_set_hash(members: list[dict]) -> str:
    """规范集合哈希：成员固定顺序（seq/skill_id/version_id），键规范化。"""
    if not members:
        return _hash({"members": []})
    norm = [{"skill_id": m["skill_id"], "version_id": m["version_id"],
             "content_hash": m["content_hash"], "seq": m["seq"]}
            for m in members]
    return _hash({"members": norm})


# 无绑定行状态的规范哈希（≠ 空集合哈希 ≠ 任意版本哈希）。
NO_BINDING_HASH = _hash({"no_binding": True})


def is_no_binding_token(expected_rev, expected_set_hash) -> bool:
    """是否首次绑定显式令牌：(FIRST_BIND_REV=0, NO_BINDING_HASH)。"""
    return (expected_rev == FIRST_BIND_REV
            and expected_set_hash == NO_BINDING_HASH)


def _current_binding_state(db, workspace_id: str) -> dict | None:
    """当前业务绑定的规范状态（供 CAS 预期与审计 from 侧）。

    返回 None（无绑定行）或 {"rev", "canonical_set_hash", "kind", "members",
    "stored_set_hash", "legacy"}。规范集合哈希 = 按当前完整成员重算的
    binding_set_hash（旧行 members_json/set_hash 为 NULL 时同样重算，
    绝不把 NULL 当作空/缺失状态用于比较）。
    集合损坏（版本缺失/哈希失配/顺序/存储哈希不一致）→ BusinessOpsError fail
    loud：不覆盖写入无法解读的状态。
    """
    st = resolve_business_binding_set(db, workspace_id, strict_schema=True)
    if st is None:
        return None
    return {"rev": st["rev"], "canonical_set_hash": st["canonical_set_hash"],
            "kind": st["kind"], "members": st["members"],
            "stored_set_hash": st["set_hash"], "legacy": st["legacy"]}


def _current_set_hash(db, workspace_id: str) -> str | None:
    """当前绑定规范集合哈希（B3 CAS 预期值；无绑定行 → None）。"""
    st = _current_binding_state(db, workspace_id)
    return st["canonical_set_hash"] if st is not None else None


def _request_fingerprint(*, action: str, workspace_id: str,
                         domain: str, target_members: list[dict],
                         expected_rev: int | None,
                         expected_set_hash: str | None,
                         evidence_ref: str | None) -> str:
    """请求级幂等指纹（同键同请求重放；同键不同请求内容拒绝）。

    覆盖：action、目标作用域（workspace_id+domain）、**完整目标成员集合**
    （规范顺序，绝不压成首成员）、**请求携带的预期修订与集合状态**
    （首次绑定 = FIRST_BIND_REV/NO_BINDING_HASH 令牌）、证据引用。

    指纹在鉴权/作用域检查后按“原始请求内容”计算；幂等键命中时直接返回该请求的
    历史结果，不重写、也不要求其预期修订仍等于当前绑定（旧预期由 CAS 语义另行
    拒绝新写入，但绝不拦截同请求重放）。
    """
    return _hash({
        "action": action, "workspace": workspace_id, "domain": domain,
        "target_members": target_members,
        "expected_rev": (int(expected_rev) if expected_rev is not None
                         else FIRST_BIND_REV),
        "expected_set_hash": expected_set_hash or NO_BINDING_HASH,
        "evidence_ref": evidence_ref or ""})


_EVENT_P53_COLS = frozenset({
    "from_members_json", "to_members_json", "from_set_hash", "to_set_hash",
    "from_rev", "to_rev"})


def _event_has_p53(db) -> bool:
    """evolution_business_events 是否已具备 P53 集合审计列（未迁移 → False）。"""
    try:
        names = {c["name"]
                 for c in sa_inspect(db.get_bind()).get_columns(EVENT_TABLE)}
    except Exception:  # noqa: BLE001
        return False
    return _EVENT_P53_COLS <= names


def _parse_members_json(raw) -> list[dict]:
    try:
        data = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        data = None
    return data if isinstance(data, list) else []


def _replay_or_fingerprint_mismatch(db, key: str, action: str, fp: str,
                                    *, expected_action: str):
    """按请求级幂等键查既有审计事件：重放返回其历史结果；同键不同内容 → 冲突。

    重放结果来自事件记录（完整 to_members/set_hash/rev），不读当前绑定状态
    （事件可能已非当前：不误报当前状态）。
    """
    if _event_has_p53(db):
        row = db.execute(sa_text(
            "SELECT action, to_version_id, to_members_json, to_set_hash, "
            "to_rev, evidence_json, reason FROM evolution_business_events "
            "WHERE idempotency_key=:k AND domain=:d "
            "ORDER BY created_at DESC LIMIT 1"),
            {"k": key, "d": DOMAIN}).fetchone()
    else:
        row = db.execute(sa_text(
            "SELECT action, to_version_id, evidence_json, reason FROM "
            "evolution_business_events WHERE idempotency_key=:k AND domain=:d "
            "ORDER BY created_at DESC LIMIT 1"),
            {"k": key, "d": DOMAIN}).fetchone()
    if row is None:
        return None
    if row.action != expected_action:
        raise BusinessOpsError(
            f"幂等键已被不同操作使用（{row.action} != {expected_action}）："
            "同键不同内容拒绝", code="idem_key_conflict", status_code=422)
    try:
        ev = json.loads(row.evidence_json or "{}")
        old_fp = ev.get("request_fingerprint")
    except (ValueError, TypeError):
        old_fp = None
    if old_fp != fp:
        raise BusinessOpsError(
            "同一幂等键携带不同请求内容（指纹不一致）：拒绝",
            code="idem_content_conflict", status_code=422)
    to_members = None
    to_set_hash = None
    to_rev = None
    if _event_has_p53(db):
        to_members = _parse_members_json(row.to_members_json)
        to_set_hash = row.to_set_hash
        to_rev = int(row.to_rev) if row.to_rev is not None else None
    return {"changed": False, "action": row.action,
            "workspace_id": None,
            "version_id": row.to_version_id,
            "members": to_members, "set_hash": to_set_hash, "rev": to_rev,
            "idempotent": True, "replay": True,
            "reason": (row.reason or "")}


def _load_accepted_member_rows(db_exp, members: list[dict],
                               workspace_id: str) -> list[dict]:
    """逐成员校验实验库版本（全部通过才返回，任一失败抛错且不落任何库）。"""
    rows = []
    for spec in members:
        ver = db_exp.execute(sa_text(
            "SELECT version_id, skill_id, seq, domain, runtime_ref, "
            "parent_version_id, skill_md, purpose_md, content_hash, source_type "
            "FROM evolution_skill_versions WHERE version_id=:v"),
            {"v": spec["version_id"]}).fetchone()
        if ver is None:
            raise BusinessOpsError(
                f"目标集合成员版本在实验库缺失: {spec['version_id']}（skill "
                f"{spec['skill_id']}，工作区 {workspace_id}）",
                code="version_not_found", status_code=404)
        expected = _hash(_canonical_payload(
            ver.skill_id, ver.domain, ver.runtime_ref, ver.skill_md,
            ver.purpose_md))
        if expected != ver.content_hash:
            raise BusinessOpsError(
                f"目标集合成员版本内容损坏（哈希失配）: {ver.version_id}",
                code="source_version_corrupt", status_code=422)
        if ver.skill_id != spec["skill_id"]:
            raise BusinessOpsError(
                f"目标成员 skill_id 与实验版本记录不一致: 声明 {spec['skill_id']}"
                f" / 记录 {ver.skill_id}（{ver.version_id}）",
                code="source_member_inconsistent")
        if spec["content_hash"] != ver.content_hash:
            raise BusinessOpsError(
                f"目标成员声明的 content_hash 与实验版本记录不一致: "
                f"{ver.version_id}（拒绝不一致集合）",
                code="source_member_inconsistent")
        if int(spec["seq"]) != int(ver.seq):
            raise BusinessOpsError(
                f"目标成员声明的 seq 与实验版本记录不一致: {ver.version_id} 记录"
                f" seq={ver.seq}，声明 seq={spec['seq']}",
                code="source_member_inconsistent")
        if ver.domain != DOMAIN or ver.runtime_ref != skill_store.RUNTIME_REF:
            raise BusinessOpsError(
                f"目标集合成员与本作用域 domain/runtime 不兼容: {ver.version_id}"
                f"（domain={ver.domain}, runtime_ref={ver.runtime_ref}）",
                code="source_member_incompatible")
        rows.append({
            "version_id": ver.version_id, "skill_id": ver.skill_id,
            "seq": int(ver.seq), "domain": ver.domain,
            "runtime_ref": ver.runtime_ref,
            "parent_version_id": ver.parent_version_id,
            "skill_md": ver.skill_md, "purpose_md": ver.purpose_md,
            "content_hash": ver.content_hash,
            "source_type": ver.source_type})
    return rows


def _spec_list(members: list[dict]) -> list[dict]:
    return [{"skill_id": m["skill_id"], "version_id": m["version_id"],
             "content_hash": m["content_hash"], "seq": int(m["seq"])}
            for m in members]


def _single_version(members: list[dict]) -> str | None:
    return members[0]["version_id"] if len(members) == 1 else None


def _promote_requires_p52(db_business) -> None:
    """正式写入口 fail closed：缺 P52 集合列即拒绝（不从 legacy 写分支绕过）。"""
    if not _binding_has_p52(db_business):
        raise BusinessOpsError(
            "业务库未迁移 P52（缺 rev/set_hash/members_json 列）：晋升/回退需要"
            "完整集合语义，先离线执行迁移；拒绝写入",
            code="binding_p52_required", status_code=503)


def promote(db_business, db_exp, exp_row, *, workspace_id: str,
            created_by: str, allow_simulated: bool = False,
            expected_set_hash: str | None = None,
            expected_rev: int | None = None,
            idempotency_key: str | None = None,
            explicit_cas: bool = False,
            target_members: list[dict] | None = None) -> dict:
    """把实验当前已接受集合晋升为业务绑定（逐成员校验→复制→集合 CAS→审计）。

    - 目标 = 完整 accepted members（弃首成员逻辑），顺序与实验注入一致
      （canonical_binding_members，(skill_id, seq) 升序）；
    - 全部成员先通过 版本存在/内容哈希/声明一致性(skill_id/content_hash/seq)/
      domain/Runtime/唯一/长度 校验，之后才复制与写入；
    - 复制、绑定切换（_materialize_set_binding，rev+set_hash CAS，首绑唯一约束）
      与审计事件同一事务；任一步失败整体回滚；
    - 审计记录完整 from/to 集合、各自集合哈希与修订、实验来源集合哈希
      （experiment-set/v1 与 business-binding-set/v1 明确分开，绝不互比）；
    - 幂等重放优先：鉴权/作用域后按幂等键识别已完成操作——同键同一原始请求
      （指纹含完整目标、作用域、预期修订/集合状态、证据引用）返回历史结果，不
      重写、也不要求旧预期修订仍等于当前；同键不同请求内容 → 拒绝；
    - explicit_cas=True（正式写入口，API 层）：必须携带显式预期令牌——当前有绑定
      行为 (expected_rev>=1, expected_set_hash=当前规范哈希)；首次绑定（无行）为
      (FIRST_BIND_REV=0, NO_BINDING_HASH)。缺字段不作任意状态；
      预期令牌原样下传到数据库层 CAS（不重读最新 rev 顶替）。
    - B1 证据缺陷仍标记开放：不宣称生产可用，也不放宽晋升资格。
    """
    _require_provisioned(db_business)
    _require_active_workspace(db_business, workspace_id)
    _promote_requires_p52(db_business)
    if explicit_cas and (expected_rev is None or expected_set_hash is None):
        raise BusinessOpsError(
            "晋升请求缺少显式预期修订/集合状态令牌（首次绑定须为 "
            f"rev=0 + NO_BINDING_HASH）：拒绝写入", code="cas_token_incomplete",
            status_code=422)
    accepted = _accepted_set(exp_row)
    exp_members = (accepted or {}).get("members") or []
    if accepted is None or not exp_members:
        raise BusinessOpsError("实验无可晋升的已接受版本", code="no_target")
    preview = promotion_preview(db_exp, exp_row, workspace_id=workspace_id,
                                allow_simulated=allow_simulated)
    if preview["blocked"]:
        raise BusinessOpsError(
            "晋升被阻止：" + "; ".join(preview["blocked"]),
            code="promotion_blocked")
    # 服务端：评分器校准状态不允许自动/业务晋升时（如 v2 engineering_only），
    # 即使证据为 real 也拒绝（非文档提示）。
    from app.core.skill_evolution.grader_registry import (
        require_business_promotable)
    try:
        require_business_promotable(
            str(getattr(exp_row, "grader_version", "") or ""))
    except Exception as exc:  # noqa: BLE001
        raise BusinessOpsError(str(exc), code="grader_not_promotable",
                               status_code=409) from exc
    # 1) 唯一转换入口：实验成员声明 → 业务规范集合（结构/重复/长度/顺序）
    members = canonical_binding_members(exp_members)
    exp_set_hash = _exp_set_hash_of(accepted)
    # 2) 逐成员全量校验（全部通过前不写任何行）
    rows = _load_accepted_member_rows(db_exp, members, workspace_id)
    spec = _spec_list(rows)
    if target_members is not None:
        # 请求显式绑定预览目标：执行时若实验“当前集合”已前移（≠预览目标），
        # 拒绝并提示重新预览——绝不悄悄改打新集合。
        try:
            requested = _spec_list(canonical_binding_members(target_members))
        except BusinessOpsError:
            raise BusinessOpsError(
                "请求目标集合非法（无法规范化为业务集合）：拒绝写入",
                code="target_invalid", status_code=422)
        if requested != spec:
            raise BusinessOpsError(
                "目标集合已变化（预览后实验接受集合前移，与提交目标不一致）：请"
                "重新预览", code="binding_conflict", status_code=409)
    target_hash = binding_set_hash(spec)
    fp = _request_fingerprint(
        action="promote", workspace_id=workspace_id, domain=DOMAIN,
        target_members=spec, expected_rev=expected_rev,
        expected_set_hash=expected_set_hash,
        evidence_ref=str(exp_row.experiment_id))
    # 3) 幂等重放优先：成功后原请求重发（哪怕预期令牌已过期/实验集合已前移）
    #    必须返回原操作结果，而不是 binding_conflict。
    if idempotency_key:
        replay = _replay_or_fingerprint_mismatch(
            db_business, idempotency_key, "promote", fp,
            expected_action="promote")
        if replay is not None:
            replay["workspace_id"] = workspace_id
            replay["version_id"] = (replay.get("version_id")
                                    or _single_version(spec))
            return replay
    # 4) CAS 语义校验（显式令牌或显式给出预期的调用）
    cur = _current_binding_state(db_business, workspace_id)
    cur_hash = cur["canonical_set_hash"] if cur is not None else None
    cur_rev = cur["rev"] if cur is not None else None
    if explicit_cas:
        if cur is None:
            if not is_no_binding_token(expected_rev, expected_set_hash):
                raise BusinessOpsError(
                    "当前无绑定，但请求预期不是首次绑定令牌（rev=0 + "
                    "NO_BINDING_HASH）：拒绝写入", code="binding_conflict",
                    status_code=409)
        else:
            if is_no_binding_token(expected_rev, expected_set_hash) or \
                    expected_rev != cur_rev or \
                    expected_set_hash != cur_hash:
                raise BusinessOpsError(
                    "预期绑定修订/集合状态与当前不一致（并发变更或预览过期，"
                    "请重新预览）：拒绝写入", code="binding_conflict",
                    status_code=409)
    else:
        if expected_set_hash is not None and expected_set_hash != cur_hash:
            raise BusinessOpsError(
                "预期绑定集合哈希与当前不一致（并发变更或预览过期）：拒绝写入",
                code="binding_conflict", status_code=409)
        if expected_rev is not None and expected_rev != cur_rev:
            raise BusinessOpsError(
                f"预期绑定修订与当前不一致（并发变更或预览过期；预期 rev="
                f"{expected_rev}，当前 {cur_rev}）：拒绝写入",
                code="binding_conflict", status_code=409)
    # already_current：当前绑定与目标集合等价（完整成员 + 内容哈希一致）
    if cur is not None and cur["kind"] == "skill" and _spec_list(
            cur["members"]) == spec:
        return {"changed": False, "action": "promote",
                "workspace_id": workspace_id,
                "version_id": spec[0]["version_id"],
                "members": spec, "set_hash": target_hash,
                "rev": cur_rev, "already_current": True}
    # 执行时资格重校验：预览与写入之间证据/集合变化 → 拒绝（不悄悄改目标）。
    fresh_report = _eligibility_report(db_exp, exp_row, accepted)
    fresh_blocks = list(fresh_report["blocked"])
    fresh_blocks.extend(_simulation_bypass_blocks(fresh_report,
                                                  allow_simulated))
    if fresh_blocks:
        raise BusinessOpsError(
            "晋升被阻止（执行时资格重校验）：" + "; ".join(fresh_blocks),
            code="promotion_blocked", status_code=409)
    idem = idempotency_key or ("promote:" + uuid.uuid4().hex)
    try:
        # 5) 复制（幂等：同版本同内容跳过；任一版本冲突 → 整体回滚）
        for r in rows:
            _copy_version_into_business(db_business, r, created_by)
        # 6) 集合 CAS 物化：DB 层护栏使用请求携带的原始预期修订/状态（显式时），
        #    而非写入前重读的最新 rev；成功 rev+1 并写 members_json/set_hash。
        if explicit_cas:
            guard_rev = expected_rev
            guard_hash = expected_set_hash
        elif expected_rev is not None or expected_set_hash is not None:
            guard_rev = expected_rev
            guard_hash = expected_set_hash
        else:
            guard_rev = cur_rev if cur is not None else None
            guard_hash = cur_hash if cur is not None else None
        materialized = _materialize_set_binding(
            db_business, workspace_id, members=spec,
            expected_rev=guard_rev, expected_set_hash=guard_hash,
            created_by=created_by)
        from_spec = _spec_list(cur["members"]) if cur else []
        from_hash = cur_hash
        from_rev = cur_rev
        # 7) 审计：完整 from/to 集合 + 各自哈希/修订 + 实验来源哈希
        _append_event(
            db_business, idem, "promote", workspace_id,
            _single_version(from_spec), _single_version(spec),
            exp_row.experiment_id, None,
            {"evidence": preview["evidence"],
             "model_mode_evidence": preview["model_mode_evidence"],
             "effect_verified": False,
             "request_fingerprint": fp,
             "expected": {"rev": expected_rev, "set_hash": expected_set_hash},
             "exp_set": {"schema_version": EXPERIMENT_SET_SCHEMA_VERSION,
                         "set_hash": exp_set_hash, "members": spec},
             "binding_schema_version": BINDING_SET_SCHEMA_VERSION,
             "conversion": "canonical_binding_members(exp_members) -> 业务绑定"
                           "集合；exp_set.members == to_members（skill_id/"
                           "version_id/content_hash 逐项一致）"},
            reason="promote accepted skill set", created_by=created_by,
            from_members=from_spec, to_members=spec,
            from_set_hash=from_hash, to_set_hash=materialized["set_hash"],
            from_rev=from_rev, to_rev=materialized["rev"])
        db_business.commit()
    except IntegrityError:
        db_business.rollback()
        if idempotency_key:
            replay = _replay_or_fingerprint_mismatch(
                db_business, idempotency_key, "promote", fp,
                expected_action="promote")
            if replay is not None:
                replay["workspace_id"] = workspace_id
                replay["version_id"] = (replay.get("version_id")
                                        or _single_version(spec))
                return replay
        raise BusinessOpsError(
            "晋升并发写入冲突（唯一约束）：拒绝覆盖",
            code="binding_conflict", status_code=409)
    except BusinessOpsError:
        db_business.rollback()
        if idempotency_key:
            replay = _replay_or_fingerprint_mismatch(
                db_business, idempotency_key, "promote", fp,
                expected_action="promote")
            if replay is not None:
                replay["workspace_id"] = workspace_id
                replay["version_id"] = (replay.get("version_id")
                                        or _single_version(spec))
                return replay
        raise
    except BaseException:
        db_business.rollback()
        raise
    return {"changed": True, "action": "promote",
            "workspace_id": workspace_id,
            "from_version_id": _single_version(from_spec),
            "version_id": spec[0]["version_id"],
            "members": spec, "set_hash": materialized["set_hash"],
            "rev": materialized["rev"], "from_members": from_spec,
            "exp_set_hash": exp_set_hash,
            "simulated_evidence": preview["model_mode_evidence"] == "simulated",
            "effect_verified": False}


def _version_spec_for_binding(db, version_id: str | None) -> list[dict]:
    """把单版本（或空）解析为绑定成员规范（测试直达/兼容路径用）。"""
    if not version_id:
        return []
    row = db.execute(sa_text(
        "SELECT version_id, skill_id, content_hash, seq FROM "
        "evolution_skill_versions WHERE version_id=:v"),
        {"v": version_id}).fetchone()
    if row is None:
        raise BusinessOpsError(f"绑定目标版本缺失: {version_id}",
                               code="binding_version_missing")
    return [{"skill_id": row.skill_id, "version_id": row.version_id,
             "content_hash": row.content_hash, "seq": int(row.seq)}]


def _materialize_set_binding(db, workspace_id: str, *, members: list[dict],
                             expected_rev: int | None,
                             expected_set_hash: str | None,
                             created_by: str) -> dict:
    """写路径唯一绑定切换点：rev+set_hash CAS + 集合物化（members_json/set_hash）。

    - members 为规范集合（[] = 显式空集合，set_kind='empty'）；skill 集合的
      兼容 pair 列 skill_id/version_id = 规范顺序首成员（满足模型
      ck_evolution_binding_pair 与旧单版本读者）；
    - 预期令牌（请求原始值，逐层下传、不在写入前用重读 rev 顶替）：
      * (FIRST_BIND_REV=0, NO_BINDING_HASH)：首次绑定（当前无行）的显式令牌；
      * (None, None)：内部/测试无条件写（仅非 explicit 调用方）；
      * 其余组合：必须等于当前行的 (rev, 规范集合哈希)。
    - 无行 → INSERT（rev=1 并物化新列）；数据库唯一约束
      ux_evolution_binding_scope(kind, workspace_id, domain) 兜底并发首绑
      竞争 → IntegrityError 转 409 binding_conflict（绝不后写覆盖）；
    - 有行 → 语义校验在 Python 侧按当前完整成员进行（旧行 set_hash/members_json
      为 NULL：读取重算哈希 ≠ 存储 NULL，不能放进普通等值 WHERE），随后受保护
      UPDATE `WHERE id AND rev=:guard_rev` 将 rev+1 并写 members_json/set_hash/
      首成员 pair 列；guard_rev = 请求预期修订（校验通过时 == 当前 rev），
      rowcount != 1 → 409（并发已在读与写之间切换绑定）；
    - 只做本次写；外层事务负责 commit/rollback。
    返回 {"rev", "set_hash", "members", "kind"}。
    """
    if not _binding_has_p52(db):
        raise BusinessOpsError(
            "业务库未迁移 P52（缺 rev/set_hash/members_json 列）：无法物化集合"
            "绑定", code="binding_p52_required", status_code=503)
    set_kind = "skill" if members else "empty"
    if len(members) > 1:
        prev = (members[0]["skill_id"], members[0]["seq"])
        for m in members[1:]:
            key = (m["skill_id"], m["seq"])
            if key <= prev:
                raise BusinessOpsError(
                    "绑定写入成员顺序非规范（须按 skill_id/seq 严格升序）",
                    code="binding_member_order")
            prev = key
    first = members[0] if members else None
    members_json = json.dumps(members, ensure_ascii=False)
    set_hash = binding_set_hash(members)
    now = datetime.now()
    row = db.execute(sa_text(
        "SELECT id, rev FROM evolution_skill_bindings "
        "WHERE kind='business' AND workspace_id=:w AND domain=:d"),
        {"w": workspace_id, "d": DOMAIN}).fetchone()
    first_bind_token = is_no_binding_token(expected_rev, expected_set_hash)
    if row is None:
        if expected_rev is not None and not first_bind_token:
            raise BusinessOpsError(
                "当前无绑定，但请求预期不是首次绑定令牌（rev=0 + "
                "NO_BINDING_HASH）：拒绝写入", code="binding_conflict",
                status_code=409)
        if expected_rev is None and expected_set_hash is not None \
                and expected_set_hash != NO_BINDING_HASH:
            raise BusinessOpsError(
                "当前无绑定，但请求预期集合状态不是 NO_BINDING_HASH：拒绝写入",
                code="binding_conflict", status_code=409)
        try:
            db.execute(sa_text(
                "INSERT INTO evolution_skill_bindings (id, kind, workspace_id, "
                "domain, set_kind, skill_id, version_id, rev, set_hash, "
                "members_json, created_by, created_at, updated_at) VALUES "
                "(:id,'business',:w,:d,:sk,:sid,:vid,1,:sh,:mj,:cb,:ca,:ua)"),
                {"id": str(uuid.uuid4()), "w": workspace_id, "d": DOMAIN,
                 "sk": set_kind,
                 "sid": first["skill_id"] if first else None,
                 "vid": first["version_id"] if first else None,
                 "sh": set_hash, "mj": members_json, "cb": created_by,
                 "ca": now, "ua": now})
        except IntegrityError:
            raise BusinessOpsError(
                "首次绑定并发竞争（唯一键冲突）：另一操作已创建绑定",
                code="binding_conflict", status_code=409)
        return {"rev": 1, "set_hash": set_hash, "members": members,
                "kind": set_kind}
    # 有行：语义校验读当前规范状态（旧 NULL 行也重算出规范哈希）。
    st = resolve_business_binding_set(db, workspace_id, strict_schema=True)
    cur_rev = st["rev"]
    cur_canonical = st["canonical_set_hash"]
    if first_bind_token:
        raise BusinessOpsError(
            "当前已存在绑定，但请求预期为首次绑定令牌（rev=0 + "
            "NO_BINDING_HASH）：拒绝覆盖", code="binding_conflict",
            status_code=409)
    if expected_rev is not None and expected_rev != cur_rev:
        raise BusinessOpsError(
            f"预期绑定修订与当前不一致（预期 rev={expected_rev}，当前 "
            f"{cur_rev}）：拒绝覆盖", code="binding_conflict", status_code=409)
    if expected_set_hash is not None and expected_set_hash != cur_canonical:
        raise BusinessOpsError(
            "预期绑定集合哈希与当前不一致（并发变更）：拒绝覆盖",
            code="binding_conflict", status_code=409)
    guard_rev = expected_rev if expected_rev is not None else cur_rev
    new_rev = cur_rev + 1
    res = db.execute(sa_text(
        "UPDATE evolution_skill_bindings SET set_kind=:sk, skill_id=:sid, "
        "version_id=:vid, rev=:nrev, set_hash=:sh, members_json=:mj, "
        "updated_at=:ua WHERE id=:id AND rev=:guard_rev"),
        {"sk": set_kind, "sid": first["skill_id"] if first else None,
         "vid": first["version_id"] if first else None, "nrev": new_rev,
         "sh": set_hash, "mj": members_json, "ua": now, "id": row.id,
         "guard_rev": guard_rev})
    if res.rowcount != 1:
        raise BusinessOpsError(
            "绑定已在并发操作中变更（rev CAS 失败）：拒绝覆盖",
            code="binding_conflict", status_code=409)
    return {"rev": new_rev, "set_hash": set_hash, "members": members,
            "kind": set_kind}


def _upsert_binding(db, workspace_id: str, version_id: str | None,
                    created_by: str) -> None:
    """无预期强制切换绑定到单版本/空集合（兼容测试/内部直达入口）。"""
    _promote_requires_p52(db)
    members = _version_spec_for_binding(db, version_id)
    _materialize_set_binding(db, workspace_id, members=members,
                             expected_rev=None, expected_set_hash=None,
                             created_by=created_by)


def _append_event(db, idem: str, action: str, workspace_id: str,
                  from_version: str | None, to_version: str | None,
                  experiment_id: str | None, run_id: str | None,
                  evidence: Any, reason: str, created_by: str, *,
                  from_members: list[dict] | None = None,
                  to_members: list[dict] | None = None,
                  from_set_hash: str | None = None,
                  to_set_hash: str | None = None,
                  from_rev: int | None = None,
                  to_rev: int | None = None) -> None:
    """审计事件写入（append-only；幂等键唯一去重）。

    P53 库：完整 from/to 集合（members_json）、各自集合哈希与修订随行保存；
    单成员集合同时镜像 from_version_id/to_version_id（旧回退链/状态查询兼容），
    多成员集合 pair 列留 NULL（完整集合在 members 列，绝不只留首版本）。
    P51 未迁移库：仅写旧列（from/to_version_id + evidence_json）。
    """
    now = datetime.now()
    if _event_has_p53(db):
        db.execute(sa_text(
            "INSERT INTO evolution_business_events (event_id, idempotency_key, "
            "action, workspace_id, domain, from_version_id, to_version_id, "
            "from_members_json, to_members_json, from_set_hash, to_set_hash, "
            "from_rev, to_rev, experiment_id, run_id, evidence_json, reason, "
            "created_by, created_at) VALUES (:eid,:k,:a,:w,:d,:fv,:tv,:fm,:tm,"
            ":fsh,:tsh,:fr,:tr,:exp,:run,:ev,:reason,:cb,:ca)"),
            {"eid": "bev_" + uuid.uuid4().hex[:20], "k": idem, "a": action,
             "w": workspace_id, "d": DOMAIN, "fv": from_version,
             "tv": to_version,
             "fm": json.dumps(from_members or [], ensure_ascii=False),
             "tm": json.dumps(to_members or [], ensure_ascii=False),
             "fsh": from_set_hash, "tsh": to_set_hash,
             "fr": from_rev, "tr": to_rev,
             "exp": experiment_id, "run": run_id,
             "ev": json.dumps(evidence, ensure_ascii=False, default=str),
             "reason": reason, "cb": created_by, "ca": now})
    else:
        db.execute(sa_text(
            "INSERT INTO evolution_business_events (event_id, idempotency_key, "
            "action, workspace_id, domain, from_version_id, to_version_id, "
            "experiment_id, run_id, evidence_json, reason, created_by, created_at) "
            "VALUES (:eid,:k,:a,:w,:d,:fv,:tv,:exp,:run,:ev,:reason,:cb,:ca)"),
            {"eid": "bev_" + uuid.uuid4().hex[:20], "k": idem, "a": action,
             "w": workspace_id, "d": DOMAIN, "fv": from_version,
             "tv": to_version, "exp": experiment_id, "run": run_id,
             "ev": json.dumps(evidence, ensure_ascii=False, default=str),
             "reason": reason, "cb": created_by, "ca": now})


def effective_history(db_business, workspace_id: str) -> list[str]:
    """按时间顺序的“已生效版本”序列（None→初始空；审计链是回退依据）。"""
    rows = db_business.execute(sa_text(
        "SELECT action, to_version_id FROM evolution_business_events "
        "WHERE workspace_id=:w AND domain=:d ORDER BY created_at, event_id"),
        {"w": workspace_id, "d": DOMAIN}).fetchall()
    seq: list[str] = []
    for r in rows:
        if r.action in ("promote", "rollback") and r.to_version_id:
            seq.append(r.to_version_id)
    return seq


def _none_state() -> dict:
    return {"kind": "none", "members": [], "hash": NO_BINDING_HASH,
            "rev": FIRST_BIND_REV}


def _set_state(members: list[dict], set_hash: str | None,
               rev: int | None) -> dict:
    spec = _spec_list(members)
    kind = "empty" if not spec else "set"
    return {"kind": kind, "members": spec,
            "hash": set_hash or binding_set_hash(spec),
            "rev": rev}


def _token_eq(a: dict, b: dict) -> bool:
    return a["kind"] == b["kind"] and a["hash"] == b["hash"]


def _state_of_event_row(db, *, version_id: str | None,
                        members: list[dict] | None,
                        set_hash: str | None, rev: int | None) -> dict:
    """把事件一侧（from/to）还原为规范状态（兼容 P53 完整集合事件与旧单版本事件，
    不编造历史：旧单版本事件按版本行重建单成员集合）。"""
    if members is not None:
        if members:
            return _set_state(members, set_hash, rev)
        if version_id:
            spec = _version_spec_for_binding(db, version_id)
            if spec:
                return _set_state(spec, None, rev)
            return _none_state()
        if set_hash == binding_set_hash([]):
            return _set_state([], set_hash, rev)
        return _none_state()
    if version_id:
        spec = _version_spec_for_binding(db, version_id)
        if spec:
            return _set_state(spec, None, rev)
        return _none_state()
    return _none_state()


def _history_of(db_business, workspace_id: str) -> list[dict]:
    """按时间顺序的有效集合状态链（起点 = 无绑定 NONE；每个 promote/rollback
    事件追加其 to 状态；兼容旧单版本事件）。用于回退：目标 = 链上最近一个
    与当前不同的状态（含合法的初始无绑定状态），绝不编造集合。"""
    if _event_has_p53(db_business):
        rows = db_business.execute(sa_text(
            "SELECT action, from_version_id, to_version_id, from_members_json, "
            "to_members_json, from_set_hash, to_set_hash, from_rev, to_rev "
            "FROM evolution_business_events WHERE workspace_id=:w AND domain=:d "
            "ORDER BY created_at, event_id"),
            {"w": workspace_id, "d": DOMAIN}).fetchall()
    else:
        rows = db_business.execute(sa_text(
            "SELECT action, from_version_id, to_version_id, from_rev, to_rev "
            "FROM evolution_business_events WHERE workspace_id=:w AND domain=:d "
            "ORDER BY created_at, event_id"),
            {"w": workspace_id, "d": DOMAIN}).fetchall()
    chain: list[dict] = [{"action": None, "to": _none_state()}]
    for r in rows:
        if r.action not in ("promote", "rollback"):
            continue
        if _event_has_p53(db_business):
            to = _state_of_event_row(
                db_business, version_id=r.to_version_id,
                members=_parse_members_json(r.to_members_json),
                set_hash=r.to_set_hash, rev=r.to_rev)
        else:
            to = _state_of_event_row(db_business, version_id=r.to_version_id,
                                     members=None, set_hash=None, rev=None)
        chain.append({"action": r.action, "to": to})
    return chain


def rollback(db_business, *, workspace_id: str, created_by: str,
             expected_set_hash: str | None = None,
             expected_rev: int | None = None,
             idempotency_key: str | None = None,
             explicit_cas: bool = False) -> dict:
    """回退：从完整审计链恢复上一“明确生效过的不同集合”（含初始无绑定状态）。

    - 目标由审计事件记录的完整 to 集合决定（支持多成员），不按版本字符串或
      首成员推断；旧单版本事件兼容为单成员集合；
    - 幂等重放优先：同键同一原始请求（作用域 + 预期修订/集合状态）返回该请求
      历史结果；重复请求不会因历史变化而自动改算另一个“上一版本”；
    - 预期令牌语义与 promote 一致（explicit_cas 正式入口）；无绑定行状态 = 无
      绑定令牌 (FIRST_BIND_REV, NO_BINDING_HASH)；
    - 写路径 = _materialize_set_binding（rev+1 + 完整 members_json/set_hash），
      审计记录完整 from/to 集合、哈希与修订；与晋升同事务；
    - 回退恢复的是业务绑定，不自动恢复已发布的 Wiki 内容。
    """
    _require_provisioned(db_business)
    _require_active_workspace(db_business, workspace_id)
    _promote_requires_p52(db_business)
    if explicit_cas and (expected_rev is None or expected_set_hash is None):
        raise BusinessOpsError(
            "回退请求缺少显式预期修订/集合状态令牌：拒绝写入",
            code="cas_token_incomplete", status_code=422)
    # 指纹只含“作用域 + 预期令牌”（目标为服务端由历史决定并记录在事件里），
    # 使同键重放不依赖历史重算。
    fp = _request_fingerprint(action="rollback", workspace_id=workspace_id,
                              domain=DOMAIN, target_members=[],
                              expected_rev=expected_rev,
                              expected_set_hash=expected_set_hash,
                              evidence_ref="history")
    if idempotency_key:
        replay = _replay_or_fingerprint_mismatch(
            db_business, idempotency_key, "rollback", fp,
            expected_action="rollback")
        if replay is not None:
            replay["workspace_id"] = workspace_id
            replay["version_id"] = replay.get("version_id")
            return replay
    cur = _current_binding_state(db_business, workspace_id)
    cur_token = _set_state(cur["members"], cur["canonical_set_hash"],
                           cur["rev"]) if cur is not None else _none_state()
    cur_hash = cur_token["hash"]
    cur_rev = cur_token["rev"]
    if explicit_cas:
        if cur is None:
            if not is_no_binding_token(expected_rev, expected_set_hash):
                raise BusinessOpsError(
                    "当前无绑定，但请求预期不是首次绑定令牌：拒绝回退",
                    code="binding_conflict", status_code=409)
        else:
            if is_no_binding_token(expected_rev, expected_set_hash) or \
                    expected_rev != cur_rev or expected_set_hash != cur_hash:
                raise BusinessOpsError(
                    "预期绑定修订/集合状态与当前不一致（请重新预览）：拒绝回退",
                    code="binding_conflict", status_code=409)
    else:
        if expected_set_hash is not None and expected_set_hash != cur_hash:
            raise BusinessOpsError(
                "预期绑定集合哈希与当前不一致（并发变更）：拒绝回退",
                code="binding_conflict", status_code=409)
        if expected_rev is not None and expected_rev != cur_rev:
            raise BusinessOpsError(
                f"预期绑定修订与当前不一致（并发变更；预期 rev={expected_rev}，"
                f"当前 {cur_rev}）：拒绝回退", code="binding_conflict",
                status_code=409)
    chain = _history_of(db_business, workspace_id)
    if not _token_eq(chain[-1]["to"], cur_token):
        raise BusinessOpsError(
            f"该作用域审计链与当前绑定不一致（当前 {cur_token['kind']}）：拒绝"
            "自动推断回退目标（不编造历史）", code="no_rollback_target")
    target_entry = None
    for entry in reversed(chain[:-1]):
        if not _token_eq(entry["to"], cur_token):
            target_entry = entry
            break
    if target_entry is None:
        raise BusinessOpsError(
            f"该作用域没有可回退的历史生效集合（当前 "
            f"{cur_token['kind']}）", code="no_rollback_target")
    target_token = target_entry["to"]
    target_members = target_token["members"]
    idem = idempotency_key or ("rollback:" + uuid.uuid4().hex)
    try:
        if explicit_cas:
            guard_rev = expected_rev
            guard_hash = expected_set_hash
        elif expected_rev is not None or expected_set_hash is not None:
            guard_rev = expected_rev
            guard_hash = expected_set_hash
        else:
            guard_rev = cur_rev if cur is not None else None
            guard_hash = cur_hash if cur is not None else None
        materialized = _materialize_set_binding(
            db_business, workspace_id, members=target_members,
            expected_rev=guard_rev, expected_set_hash=guard_hash,
            created_by=created_by)
        from_spec = _spec_list(cur["members"]) if cur else []
        _append_event(
            db_business, idem, "rollback", workspace_id,
            _single_version(from_spec), _single_version(target_members),
            None, None,
            {"reason": "rollback to previously effective set",
             "effect_verified": False, "request_fingerprint": fp,
             "expected": {"rev": expected_rev, "set_hash": expected_set_hash}},
            "rollback business binding", created_by,
            from_members=from_spec, to_members=target_members,
            from_set_hash=cur_hash if cur is not None else None,
            to_set_hash=materialized["set_hash"],
            from_rev=cur_rev if cur is not None else None,
            to_rev=materialized["rev"])
        db_business.commit()
    except IntegrityError:
        db_business.rollback()
        if idempotency_key:
            replay = _replay_or_fingerprint_mismatch(
                db_business, idempotency_key, "rollback", fp,
                expected_action="rollback")
            if replay is not None:
                replay["workspace_id"] = workspace_id
                return replay
        raise BusinessOpsError(
            "回退并发写入冲突（唯一约束）：拒绝覆盖",
            code="binding_conflict", status_code=409)
    except BusinessOpsError:
        db_business.rollback()
        if idempotency_key:
            replay = _replay_or_fingerprint_mismatch(
                db_business, idempotency_key, "rollback", fp,
                expected_action="rollback")
            if replay is not None:
                replay["workspace_id"] = workspace_id
                return replay
        raise
    except BaseException:
        db_business.rollback()
        raise
    return {"changed": True, "action": "rollback",
            "workspace_id": workspace_id,
            "from_version_id": _single_version(from_spec),
            "version_id": _single_version(target_members),
            "members": target_members, "set_hash": materialized["set_hash"],
            "rev": materialized["rev"]}


def business_state(db_business, workspace_id: str) -> dict:
    prov = schema_provisioned(db_business)
    current = None
    history: list[str] = []
    cur_hash = NO_BINDING_HASH
    no_binding = True
    set_history: list[dict] = []
    if prov:
        try:
            st = resolve_business_binding_set(db_business, workspace_id)
            if st is None:
                no_binding = True
                cur_hash = NO_BINDING_HASH
            else:
                no_binding = False
                cur_hash = st["canonical_set_hash"]
                if st["kind"] == "skill":
                    first = st["members"][0]
                    current = {
                        "version_id": first["version_id"],
                        "skill_id": first["skill_id"],
                        "content_hash": first["content_hash"],
                        "kind": st["kind"], "rev": st["rev"],
                        "set_hash": st["canonical_set_hash"],
                        "members": _spec_list(st["members"]),
                        "legacy": st["legacy"]}
                else:  # 显式空集合
                    current = {"kind": "empty", "rev": st["rev"],
                               "set_hash": st["canonical_set_hash"],
                               "members": [], "legacy": st["legacy"]}
            set_history = [{"kind": e["to"]["kind"],
                            "members": [m["version_id"]
                                        for m in e["to"]["members"]],
                            "set_hash": e["to"]["hash"]}
                           for e in _history_of(db_business, workspace_id)]
        except BusinessOpsError as exc:
            current = {"error": exc.code, "message": exc.message}
        history = effective_history(db_business, workspace_id)
    return {"provisioned": prov,
            "domain": DOMAIN,
            "current": current,
            "current_set_hash": cur_hash,
            "no_binding": no_binding,
            "expected_rev": (FIRST_BIND_REV if no_binding
                             else (current or {}).get("rev")),
            "expected_set_hash": cur_hash,
            "effective_history": history,
            "effective_set_history": set_history}
