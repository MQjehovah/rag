"""执行记录（Trace）：JSONL 追加事件 + 结束后封存（seal hash）。

- 执行中通过 emit 追加事件（模型调用/图谱隔离事件等）；
- 结束后写 meta.json、stage/输出事件，并封存：seal = sha256(meta 规范文本 +
  全部事件行)，写入 seal.sha256；
- 封存失败或输出缺失 → TraceError（不标成功）；
- 重跑创建新 execution_id 与 run 目录，绝不覆盖历史；
- 不保存认证头 / API key（本阶段记录的 messages/response 均为编译提示词与结果）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from app.core.skill_evolution.contracts import canonical_json
from app.core.skill_evolution.errors import TraceError

META_FILENAME = "meta.json"
EVENTS_FILENAME = "events.jsonl"
SEAL_FILENAME = "seal.sha256"


class TraceStore:
    """单个 execution 的记录写入器。"""

    def __init__(self, run_dir: Path, meta: dict):
        self.run_dir = Path(run_dir)
        self.meta = dict(meta)
        self._seq = 0
        self._closed = False
        try:
            self._events = open(self.run_dir / EVENTS_FILENAME, "w", encoding="utf-8")
        except OSError as exc:
            raise TraceError(f"无法创建事件文件: {exc}") from exc
        self.append({"kind": "start", "meta": self.meta})

    def append(self, event: dict) -> None:
        if self._closed:
            raise TraceError("trace 已封存，禁止继续追加")
        self._seq += 1
        record = {"seq": self._seq, **event}
        try:
            self._events.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            raise TraceError(f"事件写入失败: {exc}") from exc

    def emit(self, event: dict) -> None:
        self.append(event)

    def seal(self, final_meta: dict | None = None) -> str:
        """封存：写 meta、关闭事件流、计算 seal hash。失败抛 TraceError。"""
        if self._closed:
            raise TraceError("重复封存")
        try:
            self._events.close()
        except OSError as exc:
            raise TraceError(f"事件流关闭失败: {exc}") from exc
        meta_path = self.run_dir / META_FILENAME
        try:
            if final_meta is not None:
                self.meta.update(final_meta)
            self.meta["sealed"] = True
            meta_path.write_text(
                json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
            events_text = (self.run_dir / EVENTS_FILENAME).read_text(encoding="utf-8")
            seal = hashlib.sha256(
                (canonical_json(self.meta) + "\n" + events_text).encode("utf-8")
            ).hexdigest()
            (self.run_dir / SEAL_FILENAME).write_text(
                f"sha256:{seal}\n", encoding="utf-8")
        except OSError as exc:
            raise TraceError(f"封存写入失败: {exc}") from exc
        self._closed = True
        return seal

    def abort(self) -> None:
        """执行中途失败：不写入 meta/封存；事件文件保持可审计但未封存。"""
        try:
            if not self._events.closed:
                self._events.close()
        except OSError:
            pass
        self._closed = True


def load_meta(run_dir: Path) -> dict:
    p = Path(run_dir) / META_FILENAME
    if not p.is_file():
        raise TraceError(f"缺少 meta.json: {run_dir}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TraceError(f"meta.json 损坏: {exc}") from exc


def verify_sealed(run_dir: Path) -> str:
    """校验封存；返回 seal 值；未封存/失配 → TraceError。"""
    run_dir = Path(run_dir)
    seal_path = run_dir / SEAL_FILENAME
    if not seal_path.is_file():
        raise TraceError(f"未封存（缺少 seal.sha256）: {run_dir}")
    meta = load_meta(run_dir)
    events_path = run_dir / EVENTS_FILENAME
    if not events_path.is_file():
        raise TraceError(f"缺少事件文件: {run_dir}")
    events_text = events_path.read_text(encoding="utf-8")
    actual = hashlib.sha256(
        (canonical_json(meta) + "\n" + events_text).encode("utf-8")).hexdigest()
    expected = seal_path.read_text(encoding="utf-8").strip().removeprefix("sha256:")
    if actual != expected:
        raise TraceError(f"封存校验失败: {run_dir}")
    return expected


def iter_events(run_dir: Path) -> Iterator[dict]:
    p = Path(run_dir) / EVENTS_FILENAME
    if not p.is_file():
        raise TraceError(f"缺少事件文件: {run_dir}")
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def list_sealed_runs(root: Path) -> list[dict]:
    """列出实验根下已封存 execution 摘要（时间倒序）。"""
    runs = Path(root) / "runs"
    if not runs.is_dir():
        return []
    out = []
    for run_dir in sorted(runs.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        try:
            meta = load_meta(run_dir)
        except TraceError:
            continue
        try:
            verify_sealed(run_dir)
        except TraceError:
            meta = {**meta, "seal_status": "broken"}
        out.append({"execution_id": run_dir.name, "meta": meta})
    return out
