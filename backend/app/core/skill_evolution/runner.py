"""阶段 1 模拟模型（fake runner）与请求记录。

- SimulatedModel：确定性模拟 LLM。输入只含（任务资料快照 + 流水线提示词消息），
  输出按 `profile` 行为脚本生成——profile 是“模型行为”模拟，与评分参考无关；
  模拟模型从不读取 reference 文件（由外部保证：物化与运行阶段不加载 reference）。
- RecordingRunner：包装任意同步 runner，把每次调用（上下文、完整消息、响应、
  估算用量、异常）追加进执行事件流；阶段 1 的“模型请求与响应记录”即在此边界
  捕获（真实供应商调用将在阶段 2 通过同一边界接入）。
- 用量字段：估算值（estimated_*）与供应商实测值分离；阶段 1 无供应商 → usage
  tokens 记 None/null，绝不记 0。
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Callable

from app.core.knowledge_compiler_v3.wiki_page_builder import LLMServiceUnavailable
from app.core.skill_evolution.errors import SkillEvolutionError

# 受支持的模拟行为（profile）。unavailable/invalid_json 用于故障注入测试，
# 与案例预期/评分参考相互独立。
KNOWN_PROFILES = (
    "faithful",
    "omit_conditions",
    "flatten_versions",
    "quadruple_numerics",
    "unavailable",
    "invalid_json",
)

# 版本化合成提示词模板特征标记（与 WIKI_SYNTHESIS_VERSIONED_PROMPT 一致）。
_VERSIONED_MARKERS = ("**版本块**", "versions 必须且只能")
# 提示词中版本来源分组标记。
_VERSION_GROUP_RE = re.compile(r"==\s*版本\s+([^=\n]+?)\s*==")


class UnsupportedContextError(SkillEvolutionError):
    """模拟模型遇到未建模的模型调用上下文（应 fail loud，不静默通过）。"""


@dataclass(frozen=True)
class SimDoc:
    doc_id: str
    title: str
    content: str
    product_version: str | None = None


@dataclass
class SimulatedModel:
    """确定性模拟模型（同步签名：call(messages, context, timeout) -> dict）。"""

    docs: tuple[SimDoc, ...]
    profile: str = "faithful"

    def __post_init__(self) -> None:
        if self.profile not in KNOWN_PROFILES:
            raise SkillEvolutionError(f"未知 runner profile: {self.profile}")

    def __call__(self, messages, context: str = "", timeout: float = 120.0) -> dict:
        self._last_messages = messages
        if self.profile == "unavailable" and context in (
            "wiki-synthesis", "wiki-batch-summary", "wiki-mapreduce", "wiki-ingest-page"):
            raise LLMServiceUnavailable(f"offline profile unavailable [{context}]")
        if context == "wiki-synthesis":
            return self._synthesis(messages)
        if context == "wiki-ingest-page":
            raise UnsupportedContextError(
                "wiki-ingest-page 不在阶段 1 数据集路径内（manual_rebuild）")
        if context in ("wiki-batch-summary", "wiki-mapreduce"):
            raise UnsupportedContextError(
                f"context {context!r} 未建模：来源超预算会走 Map-Reduce，"
                "阶段 1 数据集应保持在单次合成预算内")
        raise UnsupportedContextError(f"未建模的模型调用上下文: {context!r}")

    # -- 内部 -------------------------------------------------------------

    def _prompt(self, messages) -> str:
        parts = []
        for m in messages or []:
            content = m.get("content") if isinstance(m, dict) else ""
            if content:
                parts.append(str(content))
        return "\n".join(parts)

    def _synthesis(self, messages) -> dict:
        prompt = self._prompt(messages)
        markers = self._markers(messages)
        if markers & {"FORCE-UNAVAILABLE"}:
            raise LLMServiceUnavailable(
                "offline profile FORCE-UNAVAILABLE [wiki-synthesis]")
        if any(marker in prompt for marker in _VERSIONED_MARKERS):
            return self._synthesis_versioned(prompt)
        return self._synthesis_plain()

    @staticmethod
    def _markers(messages) -> set[str]:
        """模拟模型识别注入技能指令中的行为标记（仅离线模拟契约）。

        标记由候选技能正文（system 指令）提供，模型据此改变输出/可用性：
        STRICT-V1=追加核对清单；WEAK-SKIP-CONDITIONS=跳过条件要点；
        FORCE-UNAVAILABLE=模拟服务不可用（invalid 评估路径）。
        真实模型不需要这些标记（也不会被注入解释）。
        """
        found: set[str] = set()
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "system":
                content = str(m.get("content") or "")
                for marker in ("STRICT-V1", "WEAK-SKIP-CONDITIONS",
                               "FORCE-UNAVAILABLE"):
                    if marker in content:
                        found.add(marker)
        return found

    @staticmethod
    def _version_sort_key(label: str):
        nums = re.findall(r"\d+", label)
        return tuple(int(n) for n in nums) if nums else (10**9, label)

    def _requested_labels(self, prompt: str) -> list[str]:
        """提示词中出现的版本分组标签（== 版本 X ==），确定性顺序。"""
        seen: list[str] = []
        for raw in _VERSION_GROUP_RE.findall(prompt):
            label = raw.strip()
            if label and label not in seen:
                seen.append(label)
        return seen

    def _doc_lines(self, docs) -> list[str]:
        lines: list[str] = []
        for doc in docs:
            lines.append(f"## {doc.title}")
            for raw in doc.content.splitlines():
                line = raw.strip()
                if line:
                    lines.append(line)
        return lines

    def _body_for(self, docs, *, drop_conditions: bool, scale: int | None) -> str:
        lines = self._doc_lines(docs)
        if drop_conditions:
            lines = [
                ln for ln in lines
                if not ln.startswith(("适用条件", "前置条件"))
            ]
        body = "\n".join(lines)
        if scale not in (None, 1):
            body = re.sub(r"(\d+)", lambda m: str(int(m.group(1)) * scale), body)
        return body

    def _summary(self) -> str:
        return "Titan 810 电池模组知识整理（离线模拟）"

    def _synthesis_plain(self) -> dict:
        if self.profile == "invalid_json":
            return {"unexpected": True}
        markers = self._markers(self._last_messages)
        content = self._body_for(
            self.docs,
            drop_conditions=(self.profile == "omit_conditions"
                             or "WEAK-SKIP-CONDITIONS" in markers),
            scale=(4 if self.profile == "quadruple_numerics" else None),
        )
        if "STRICT-V1" in markers:
            content += "\n\n# 核对清单\n- 已核对适用/前置条件来源（STRICT-V1）"
        return {"summary": self._summary(), "content": content}

    def _synthesis_versioned(self, prompt: str) -> dict:
        if self.profile == "invalid_json":
            return {"unexpected": True}
        markers = self._markers(self._last_messages)
        if "STRICT-V1" in markers:
            return {"summary": self._summary(),
                    "common": "已核对适用/前置条件来源（STRICT-V1）",
                    "versions": [], "unversioned": ""}
        if "WEAK-SKIP-CONDITIONS" in markers:
            raise LLMServiceUnavailable("offline WEAK versioned unsupported")
        requested = self._requested_labels(prompt)
        by_version: dict[str, list[SimDoc]] = {}
        for doc in self.docs:
            v = doc.product_version
            if v:
                by_version.setdefault(v, []).append(doc)
        flatten = self.profile == "flatten_versions"
        drop_conditions = self.profile == "omit_conditions"
        scale = 4 if self.profile == "quadruple_numerics" else None

        versions = []
        for label in requested:
            group = list(self.docs) if flatten else by_version.get(label, [])
            if not group:
                continue
            content = self._body_for(
                group, drop_conditions=drop_conditions, scale=scale)
            notice = ""
            if not flatten and len(by_version.get(label, [])) > 1:
                notice = "该版本信息存在差异"
            versions.append({
                "version": label,
                "content": content,
                "diff_notice": notice,
            })
        return {
            "summary": self._summary(),
            "common": "",
            "versions": versions,
            "unversioned": "",
        }


def estimate_chars(messages, response) -> tuple[int, int]:
    """估算输入/输出字符数（不依赖供应商 usage 字段）。"""
    in_chars = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else ""
        in_chars += len(str(content or ""))
    out = response
    if not isinstance(out, (str, bytes)):
        out = json.dumps(out, ensure_ascii=False, default=str)
    return in_chars, len(str(out))


def estimate_tokens(chars: int) -> int:
    """粗略 token 估算（中英混合取 2 字符/token），仅为工程成本参照。"""
    return max(1, (chars + 1) // 2)


class RecordingRunner:
    """把每次模型调用与响应（或异常）追加进事件流，然后透传结果。"""

    def __init__(self, inner: Callable, emit: Callable[[dict], None]):
        self._inner = inner
        self._emit = emit
        self._seq = 0
        self.call_count = 0

    def __call__(self, messages, context: str = "", timeout: float = 120.0):
        self._seq += 1
        self.call_count += 1
        messages_copy = copy.deepcopy(messages)
        seq = self._seq
        try:
            result = self._inner(messages_copy, context=context, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            in_chars, _ = estimate_chars(messages_copy, {})
            self._emit({
                "kind": "model_call",
                "seq": seq,
                "context": context,
                "messages": messages_copy,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "response": None,
                "estimated_input_chars": in_chars,
                "estimated_output_chars": 0,
                "usage_tokens": None,
            })
            raise
        in_chars, out_chars = estimate_chars(messages_copy, result)
        self._emit({
            "kind": "model_call",
            "seq": seq,
            "context": context,
            "messages": messages_copy,
            "response": result,
            "estimated_input_chars": in_chars,
            "estimated_output_chars": out_chars,
            "estimated_tokens": estimate_tokens(in_chars + out_chars),
            "usage_tokens": None,  # 阶段 1 无供应商用量 → null，非 0
        })
        return result


class GraphRecorder:
    """隔离 graph runner：只记录目标、不写任何生产/图谱资源。"""

    def __init__(self, emit: Callable[[dict], None]):
        self._emit = emit

    def __call__(self, *, wiki_page_id=None, page_id=None,
                 remove_page=False, remove_wiki=False) -> bool:
        self._emit({
            "kind": "graph_isolated",
            "wiki_page_id": wiki_page_id,
            "page_id": page_id,
            "remove_page": remove_page,
            "remove_wiki": remove_wiki,
        })
        return True
