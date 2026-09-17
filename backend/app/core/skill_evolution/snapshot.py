"""输入快照存储（阶段 1）：保存重放所需资料全文 + 元数据，不可覆盖、内容寻址、封存。

- 快照保存来源资料全文（content）与标题/版本元数据 —— 不能只保存内容哈希；
- 同一快照标识（label）已存在只允许内容一致（校验后幂等返回）；不同内容复用同一
  标识 → SnapshotError（不可覆盖）；
- 相同内容在不同 label 下可各自存在；
- dataset 原始资料变化不影响已封存快照 → 重放仍一致（重放只读 snapshot 副本）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.skill_evolution.contracts import canonical_json
from app.core.skill_evolution.errors import SnapshotError

SNAPSHOT_DIR_NAME = "snapshots"
_SOURCES_FILE = "sources.json"
_META_FILE = "meta.json"
_SEAL_FILE = "seal.sha256"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SnapshotDoc:
    """快照内的一份来源资料（含全文与元数据）。"""

    doc_id: str
    title: str
    content: str
    file: str = ""
    product_version: str | None = None

    def to_dict(self) -> dict:
        out = {
            "doc_id": self.doc_id,
            "title": self.title,
            "content": self.content,
            "file": self.file,
        }
        if self.product_version:
            out["product_version"] = self.product_version
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "SnapshotDoc":
        return cls(
            doc_id=str(data["doc_id"]),
            title=str(data["title"]),
            content=str(data.get("content") or ""),
            file=str(data.get("file") or ""),
            product_version=(data.get("product_version") or None),
        )


@dataclass(frozen=True)
class SnapshotContent:
    """一个快照的可重放内容。"""

    label: str
    wiki_title: str
    wiki_category: str
    docs: tuple[SnapshotDoc, ...] = ()
    meta: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return {
            "label": self.label,
            "wiki": {"title": self.wiki_title, "category": self.wiki_category},
            "docs": [d.to_dict() for d in self.docs],
        }

    def content_hash(self) -> str:
        return _sha256(canonical_json(self.payload()))


class SnapshotStore:
    """root/snapshots/<label>/ 目录管理器。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / SNAPSHOT_DIR_NAME

    def store(self, content: SnapshotContent) -> SnapshotContent:
        """写入快照；label 冲突且内容不一致 → SnapshotError（不可覆盖）。"""
        snapshot_hash = content.content_hash()
        target = self.dir / content.label
        if target.exists():
            existing = self.load(content.label)
            if existing.content_hash() != snapshot_hash:
                raise SnapshotError(
                    f"snapshot_id {content.label!r} 已存在但内容不一致（不可覆盖）"
                )
            return content
        try:
            target.mkdir(parents=True)
            docs_text = json.dumps(
                [d.to_dict() for d in content.docs], ensure_ascii=False, indent=2)
            (target / _SOURCES_FILE).write_text(docs_text, encoding="utf-8")
            meta = {
                "snapshot_id": content.label,
                "content_hash": snapshot_hash,
                "wiki": {"title": content.wiki_title, "category": content.wiki_category},
                **content.meta,
            }
            (target / _META_FILE).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            seal = self._compute_seal(meta, docs_text)
            (target / _SEAL_FILE).write_text(f"sha256:{seal}\n", encoding="utf-8")
        except OSError as exc:  # 部分写入后清理，避免残留“半封存”快照
            import shutil
            shutil.rmtree(target, ignore_errors=True)
            raise SnapshotError(f"快照写入失败: {exc}") from exc
        return content

    @staticmethod
    def _compute_seal(meta: dict, docs_text: str) -> str:
        return _sha256(canonical_json(meta) + "\n" + docs_text)

    def load(self, label: str) -> SnapshotContent:
        """读取并校验封存哈希；损坏/缺失 → SnapshotError（不静默忽略）。"""
        target = self.dir / label
        if not target.is_dir():
            raise SnapshotError(f"快照缺失: {label}")
        seal_path = target / _SEAL_FILE
        if not seal_path.is_file():
            raise SnapshotError(f"快照未封存: {label}")
        stored = seal_path.read_text(encoding="utf-8").strip()
        content = self._read_payload(target)
        docs_text = json.dumps(
            [d.to_dict() for d in content.docs], ensure_ascii=False, indent=2)
        actual = self._compute_seal(content.meta, docs_text)
        expected = stored.removeprefix("sha256:")
        if actual != expected:
            raise SnapshotError(f"快照封存校验失败: {label}")
        return content

    def _read_payload(self, target: Path) -> SnapshotContent:
        try:
            docs_raw = json.loads((target / _SOURCES_FILE).read_text(encoding="utf-8"))
            meta = json.loads((target / _META_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"快照文件损坏: {target.name}: {exc}") from exc
        wiki = meta.get("wiki") or {}
        return SnapshotContent(
            label=str(meta.get("snapshot_id") or ""),
            wiki_title=str(wiki.get("title") or ""),
            wiki_category=str(wiki.get("category") or ""),
            docs=tuple(SnapshotDoc.from_dict(d) for d in docs_raw),
            meta=meta,
        )

    def exists(self, label: str) -> bool:
        return (self.dir / label).is_dir()
