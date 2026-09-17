"""阶段 2/5 指令注入：冻结技能集合（FrozenSkillSet）+ 单边界注入 runner。

边界（阶段 0 复核一致）：default 编译模型调用经 ctx llm_runner 单点；只对内容生成
上下文（wiki-synthesis / wiki-batch-summary / wiki-mapreduce）注入一次；其余透传。

阶段 5 多技能集合契约：
- 支持 空集合 / 单技能 / 多技能；
- 每个 skill_id 最多一个版本（重复拒绝）；
- 成员顺序固定（按 skill_id，其次 seq）；
- 集合哈希 = sha256(规范 JSON{"members":[{"skill_id","version_id","content_hash"}…]})，
  包含有序成员与版本内容；空集合用 EMPTY_SET_HASH（sha256('[]')）；
- 单次模型调用中每技能只注入一次（单条 system 消息内按序拼接）；
- 总指令长度超 MAX_SET_TEXT_CHARS → 明确失败（不静默截断）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from app.core.skill_evolution.contracts import canonical_json
from app.core.skill_evolution.errors import SkillEvolutionError

CONTENT_CONTEXTS = frozenset(("wiki-synthesis", "wiki-batch-summary", "wiki-mapreduce"))
EMPTY_SET_HASH = hashlib.sha256("[]".encode("utf-8")).hexdigest()
MAX_SET_TEXT_CHARS = 40_000


class SkillSetError(SkillEvolutionError):
    """技能集合结构/长度错误。"""


def is_content_generation_context(context: str) -> bool:
    """唯一的内容生成 context 判定（实验与业务共用）。"""
    return str(context or "") in CONTENT_CONTEXTS


def _member_as_dict(row) -> dict:
    if isinstance(row, Mapping):
        return {
            "skill_id": str(row.get("skill_id") or ""),
            "version_id": str(row.get("version_id") or ""),
            "content_hash": str(row.get("content_hash") or ""),
            "seq": int(row.get("seq") or 0),
            "skill_md": str(row.get("skill_md") or ""),
        }
    return {
        "skill_id": str(getattr(row, "skill_id", "") or ""),
        "version_id": str(getattr(row, "version_id", "") or ""),
        "content_hash": str(getattr(row, "content_hash", "") or ""),
        "seq": int(getattr(row, "seq", 0) or 0),
        "skill_md": str(getattr(row, "skill_md", "") or ""),
    }


def render_execution_skill_text(members: Sequence) -> str:
    """唯一的技能执行正文：仅规范化 SKILL.md，不含 PURPOSE.md 或额外包装。

    成员按 (skill_id, seq) 排序；超 MAX_SET_TEXT_CHARS 明确失败，禁止截断。
    """
    if not members:
        return ""
    specs = [_member_as_dict(m) for m in members]
    seen: set[str] = set()
    for m in specs:
        if not m["skill_id"] or not m["version_id"]:
            raise SkillSetError("技能执行正文缺少 skill_id/version_id")
        if m["skill_id"] in seen:
            raise SkillSetError(
                f"技能集合中 skill_id 重复（每技能最多一个版本）: {m['skill_id']}")
        seen.add(m["skill_id"])
    specs.sort(key=lambda m: (m["skill_id"], m["seq"]))
    parts = [
        f"# 技能指令 {m['skill_id']}（版本 {m['version_id']}）\n\n{m['skill_md']}"
        for m in specs
    ]
    text = "\n\n".join(parts)
    if len(text) > MAX_SET_TEXT_CHARS:
        raise SkillSetError(
            f"技能集合指令总长度超限（{len(text)} > {MAX_SET_TEXT_CHARS}，"
            "不静默截断）")
    return text


def build_injected_messages(
    messages, context: str, *, instruction_text: str | None, mode: str,
) -> tuple[list, bool, str]:
    """唯一的 system message 构造：返回 (messages, injected, reason)。"""
    if mode == "versions" and is_content_generation_context(context):
        if not instruction_text:
            raise SkillSetError("versions 模式缺少执行指令正文（不静默跳过）")
        injected_messages = (
            [{"role": "system", "content": instruction_text}]
            + list(messages or [])
        )
        return injected_messages, True, "content_generation"
    if mode == "versions":
        reason = "context_not_content"
    elif mode == "empty":
        reason = "empty_set"
    else:
        reason = "disabled"
    return list(messages or []), False, reason


def _member_key(row) -> tuple:
    seq = getattr(row, "seq", None)
    if seq is None:
        seq = 0
    return (str(getattr(row, "skill_id", "")), int(seq or 0))


def _member_specs(rows: Sequence) -> tuple[dict, ...]:
    out = []
    seen: set[str] = set()
    for r in rows:
        skill_id = str(getattr(r, "skill_id", ""))
        version_id = str(getattr(r, "version_id", ""))
        content_hash = str(getattr(r, "content_hash", ""))
        if not skill_id or not version_id or not content_hash:
            raise SkillSetError("技能集合成员缺少 skill_id/version_id/content_hash")
        if skill_id in seen:
            raise SkillSetError(
                f"技能集合中 skill_id 重复（每技能最多一个版本）: {skill_id}")
        seen.add(skill_id)
        out.append({
            "skill_id": skill_id,
            "version_id": version_id,
            "content_hash": content_hash,
            "seq": int(getattr(r, "seq", 0) or 0),
        })
    out.sort(key=lambda m: (m["skill_id"], m["seq"]))
    return tuple(out)


def set_hash_for_members(members: Sequence[dict]) -> str:
    ordered = sorted(
        ({k: m[k] for k in ("skill_id", "version_id", "content_hash")}
         for m in members),
        key=lambda m: (m["skill_id"], m["content_hash"]))
    return hashlib.sha256(
        canonical_json({"members": list(ordered)}).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrozenSkillSet:
    """一次执行启动时冻结的技能集合（不可变；不随绑定切换变化）。"""

    mode: str  # 'none' | 'empty' | 'versions'
    version_ids: tuple[str, ...] = ()
    content_hashes: tuple[str, ...] = ()
    set_hash: str | None = None
    instruction_text: str | None = None
    source_versions: tuple = ()
    members: tuple = ()

    @classmethod
    def disabled(cls) -> "FrozenSkillSet":
        return cls(mode="none")

    @classmethod
    def empty(cls) -> "FrozenSkillSet":
        return cls(mode="empty", set_hash=EMPTY_SET_HASH)

    @classmethod
    def from_versions(cls, rows: Sequence) -> "FrozenSkillSet":
        """由校验过的版本行构造（每 skill 一个版本、固定顺序、总长上限）。"""
        if not rows:
            return cls.empty()
        ordered = sorted(rows, key=_member_key)
        specs = _member_specs(ordered)
        ids = tuple(m["version_id"] for m in specs)
        hashes = tuple(m["content_hash"] for m in specs)
        text = render_execution_skill_text(ordered)
        return cls(
            mode="versions",
            version_ids=ids,
            content_hashes=hashes,
            set_hash=set_hash_for_members(specs),
            instruction_text=text,
            source_versions=tuple(ordered),
            members=specs,
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "version_ids": list(self.version_ids),
            "content_hashes": list(self.content_hashes),
            "set_hash": self.set_hash,
            "members": list(self.members),
            "injected_text_present": bool(self.instruction_text),
            "instruction_chars": len(self.instruction_text or ""),
        }

    def summary(self) -> str:
        if self.mode == "versions":
            return "versions:" + ",".join(self.version_ids)
        return f"mode:{self.mode}"


class SkillInjectingRunner:
    """把冻结指令插入内容生成模型调用的最外层包装（唯一注入点）。"""

    def __init__(self, plan: FrozenSkillSet, inner: Callable,
                 emit: Callable[[dict], None]):
        self._plan = plan
        self._inner = inner
        self._emit = emit

    def __call__(self, messages, context: str = "", timeout: float = 120.0):
        plan = self._plan
        injected_messages, injected, reason = build_injected_messages(
            messages, context, instruction_text=plan.instruction_text,
            mode=plan.mode)
        self._emit({
            "kind": "skill_injection",
            "mode": plan.mode,
            "version_ids": list(plan.version_ids),
            "content_hashes": list(plan.content_hashes),
            "set_hash": plan.set_hash,
            "members": list(plan.members),
            "context": context,
            "injected": injected,
            "reason": reason,
        })
        return self._inner(injected_messages, context=context, timeout=timeout)
