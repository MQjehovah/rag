"""阶段 7A：对照实验协议、隔离计划、离线 dry-run、冻结后测试与统计。

- 四组协议：A 无技能 / B 固定初始技能 / C 无持久经验 Wiki / D 完整 WikiSkill；
  C/D 除“经验机制”外共享初始化、任务、执行/提议模型、工具权限、门控与停止规则。
- 实验隔离：每协议每 replicate 独立运行目录/数据库/产物；不允许继承先前演化结果；
  无随机 seed 时如实记录（复制/缓存重放不计为独立运行）。
- 正式测试（test）绝不进入训练/维护/提议/门控回路；所有组冻结最终技能集合后，
  才调用 evaluate_frozen_test。测试结果用于调参 → 必须标记该测试集已被消费。
- 主指标 passed_over_total_v1 不变；预算估算只给“调用次数/轮数/角色”口径，
  无价格/usage 时 cost=None（不编造金额）。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.skill_evolution.adapter import run_one
from app.core.skill_evolution.contracts import DatasetSpec, canonical_json, load_dataset
from app.core.skill_evolution.grader import grade as grade_task
from app.core.skill_evolution.injector import FrozenSkillSet

PROTOCOL_A = "A"
PROTOCOL_B = "B"
PROTOCOL_C = "C"
PROTOCOL_D = "D"
PROTOCOLS = (PROTOCOL_A, PROTOCOL_B, PROTOCOL_C, PROTOCOL_D)

# 每组建议独立运行次数（完整/无经验组至少 3；A/B 至少 1 作为基线）。
RECOMMENDED_RUNS = {PROTOCOL_A: 1, PROTOCOL_B: 1, PROTOCOL_C: 3, PROTOCOL_D: 3}


@dataclass(frozen=True)
class ProtocolConfig:
    """某一次实验的完整配置（写入每 replicate 目录，禁止自动猜测）。"""

    protocol: str
    replicate: int
    init_mode: str            # empty / seed（paper → empty；business → seed）
    seed_skill_versions: tuple[str, ...] = ()
    evolve: bool = True
    experience: str = "full"  # none / full（C=none, D=full）
    iterations: int = 3
    dataset_version: str = "wiki-default-v4"
    grader_version: str = "wiki-default-grader/v1"
    model_mode: str = "simulated"   # simulated / real（real 缺配置失败不回退）
    model_ref: str | None = None
    prompt_version: str = "wiki-synthesis/v3+v4-instructions-v1"
    timeout: float = 120.0
    budget: dict = field(default_factory=lambda: {
        "max_model_calls": 200, "max_tool_calls": 200, "max_seconds": 3600,
        "max_iterations": 3})
    # C 保留哪些非 Wiki 信息（对齐 D，防历史摘要成为隐藏经验 Wiki）
    keep_non_wiki_feedback: bool = True   # gate 历史/提案历史/技能历史
    seed_content_hash: str | None = None
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "protocol": self.protocol, "replicate": self.replicate,
            "init_mode": self.init_mode,
            "seed_skill_versions": list(self.seed_skill_versions),
            "evolve": self.evolve, "experience": self.experience,
            "iterations": self.iterations, "dataset_version": self.dataset_version,
            "grader_version": self.grader_version, "model_mode": self.model_mode,
            "model_ref": self.model_ref, "prompt_version": self.prompt_version,
            "timeout": self.timeout, "budget": dict(self.budget),
            "keep_non_wiki_feedback": self.keep_non_wiki_feedback,
            "seed_content_hash": self.seed_content_hash,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProtocolConfig":
        return cls(protocol=d["protocol"], replicate=d["replicate"],
                   init_mode=d["init_mode"],
                   seed_skill_versions=tuple(d.get("seed_skill_versions") or []),
                   evolve=bool(d.get("evolve", True)),
                   experience=d.get("experience", "full"),
                   iterations=int(d.get("iterations", 3)),
                   dataset_version=d.get("dataset_version", "wiki-default-v4"),
                   grader_version=d.get("grader_version",
                                        "wiki-default-grader/v1"),
                   model_mode=d.get("model_mode", "simulated"),
                   model_ref=d.get("model_ref"),
                   prompt_version=d.get("prompt_version", "wiki-synthesis/v3+v4"),
                   timeout=float(d.get("timeout", 120.0)),
                   budget=dict(d.get("budget") or {}),
                   keep_non_wiki_feedback=bool(d.get("keep_non_wiki_feedback", True)),
                   seed_content_hash=d.get("seed_content_hash"),
                   notes=d.get("notes", ""))


SEED_DEFAULT_DIR = (
    Path(__file__).resolve().parents[3] / "eval/wiki_evolution/skills/seed-default-v1"
)
FROZEN_SET_SCHEMA = "frozen-skill-set/v1"
FROZEN_SET_FILENAME = "final-skill-set.json"
TEST_STATE_SCHEMA = "batch-test-state/v2"
TEST_STATE_FILENAME = "batch-test-state.json"
TEST_STATE_SEAL_FILENAME = "batch-test-state.sha256"
TEST_BUDGET_LAB = Path("test-budget") / "lab"
BATCH_TEST_LEASE_OWNER = "batch-test"
TEST_STATE_REQUIRED = (
    "schema", "dataset_version", "manifest_plan_hash", "config_fingerprint",
    "test_confirm_fingerprint", "budget", "run_id", "experiment_id", "lab",
    "items", "completed", "reports", "report_hashes",
)

# 测试注入：renewer.lost 检查之后、completed CAS 之前。生产必须保持 None。
before_completed_state_commit = None
last_test_state_cas_rowcount: int | None = None


def seed_package_fingerprints(seed_dir: Path | None = None) -> dict:
    """解析内置种子包（SKILL.md + PURPOSE.md）的非秘密指纹；不可解析则失败。"""
    seed_dir = Path(seed_dir or SEED_DEFAULT_DIR)
    skill_path = seed_dir / "SKILL.md"
    purpose_path = seed_dir / "PURPOSE.md"
    if not skill_path.is_file() or not purpose_path.is_file():
        raise ValueError(f"无法解析初始技能包（缺 SKILL.md/PURPOSE.md）: {seed_dir}")
    from app.core.skill_evolution import skill_store
    pkg = skill_store.load_package(seed_dir, source_type="builtin_seed")
    return {
        "version_id": f"{pkg.skill_id}:0001",
        "skill_id": pkg.skill_id,
        "content_hash": pkg.content_hash(),
        "skill_md_sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        "purpose_md_sha256": hashlib.sha256(purpose_path.read_bytes()).hexdigest(),
        "seed_dir": str(seed_dir),
    }


def default_protocol_config(protocol: str, replicate: int, *,
                            init_mode: str = "paper",
                            dataset_version: str | None = None,
                            grader_version: str | None = None) -> ProtocolConfig:
    """按协议给出显式配置。A/B 不进化；C/D 除 experience 外一致。"""
    if protocol not in PROTOCOLS:
        raise ValueError(f"未知协议: {protocol}")
    evolve = protocol in (PROTOCOL_C, PROTOCOL_D)
    experience = ("none" if protocol == PROTOCOL_C else
                  "full" if protocol == PROTOCOL_D else "full")
    seed_versions: tuple[str, ...] = ()
    seed_hash = None
    if protocol == PROTOCOL_B:
        meta = seed_package_fingerprints()
        seed_versions = (meta["version_id"],)
        seed_hash = meta["content_hash"]
    kwargs = dict(
        protocol=protocol, replicate=replicate,
        init_mode="empty" if init_mode == "paper" else "seed",
        evolve=evolve,
        experience=experience,
        iterations=(1 if not evolve else 3),
        seed_skill_versions=seed_versions,
        seed_content_hash=seed_hash,
    )
    if dataset_version:
        kwargs["dataset_version"] = dataset_version
    if grader_version:
        kwargs["grader_version"] = grader_version
    if protocol == PROTOCOL_A:
        kwargs["experience"] = "none"
        kwargs["init_mode"] = "empty"
    if protocol == PROTOCOL_B:
        kwargs["init_mode"] = "seed"
        kwargs["experience"] = "none"
    return ProtocolConfig(**kwargs)


def comparable_protocol_fields(cfg: ProtocolConfig) -> dict:
    """C/D 逐字段比较时忽略的键只有 experience。"""
    d = cfg.to_dict()
    d.pop("experience", None)
    d.pop("protocol", None)
    d.pop("replicate", None)
    d.pop("notes", None)
    return d


def check_plan(dataset: DatasetSpec, cfg: ProtocolConfig) -> list[str]:
    """配置与数据契约校验；返回错误列表（空=通过）。"""
    errors: list[str] = []
    if cfg.protocol not in PROTOCOLS:
        errors.append(f"protocol 非法: {cfg.protocol}")
    if dataset.dataset_version != cfg.dataset_version:
        errors.append("数据集版本与配置不一致")
    if dataset.grader_version != cfg.grader_version:
        errors.append("grader_version 与配置不一致")
    if cfg.protocol == PROTOCOL_A and cfg.evolve:
        errors.append("A 组必须 evolve=false")
    if cfg.protocol == PROTOCOL_B and cfg.evolve:
        errors.append("B 组必须 evolve=false")
    if cfg.protocol in (PROTOCOL_C, PROTOCOL_D) and not cfg.evolve:
        errors.append(f"{cfg.protocol} 组必须 evolve=true")
    if cfg.protocol == PROTOCOL_A and cfg.seed_skill_versions:
        errors.append("A 组不得绑定初始技能")
    if cfg.protocol == PROTOCOL_B:
        if not cfg.seed_skill_versions:
            errors.append("B 组必须绑定可解析的初始技能版本")
        else:
            try:
                meta = seed_package_fingerprints()
            except ValueError as exc:
                errors.append(str(exc))
            else:
                if cfg.seed_skill_versions[0] != meta["version_id"]:
                    errors.append("B 组 seed 版本无法解析为内置种子包")
                if cfg.seed_content_hash and cfg.seed_content_hash != meta["content_hash"]:
                    errors.append("B 组 seed content_hash 与包不一致")
    if cfg.evolve and cfg.experience == "full" and cfg.iterations < 1:
        errors.append("完整组迭代数必须 >=1")
    if cfg.model_mode == "real" and cfg.model_ref is None:
        errors.append("real 模式需要显式 model_ref（不能猜测供应商 model ID）")
    train_by_group: dict[str, set[str]] = {}
    for t in dataset.tasks:
        if t.split == "train":
            train_by_group.setdefault(t.group_id, set()).add(t.task_id)
    if cfg.evolve:
        ok_any = any(len(v) >= 4 for v in train_by_group.values())
        if not ok_any:
            errors.append("没有 ≥4 个不同训练任务的组（提议者无法读取 4 条）")
    return errors


# ---------------------------------------------------------------------------
# 计划 / 隔离 / 估算
# ---------------------------------------------------------------------------


def build_plan(root: Path, dataset_version: str,
               runs: dict[str, int] | None = None,
               *, dataset: DatasetSpec | None = None) -> list[dict]:
    """生成 batch 计划：每协议每 replicate 一个独立运行目录与显式配置。"""
    root = Path(root)
    plan_root = root / "plan"
    plan_root.mkdir(parents=True, exist_ok=True)
    runs = runs or RECOMMENDED_RUNS
    ds = dataset
    grader_version = None
    if ds is None:
        # 仅版本字符串时仍写入 protocol；完整校验由 create_batch 执行。
        grader_version = None
    else:
        if ds.dataset_version != dataset_version:
            raise ValueError("build_plan: dataset.dataset_version 与参数不一致")
        grader_version = ds.grader_version
    plan = []
    for proto in PROTOCOLS:
        n = runs.get(proto, RECOMMENDED_RUNS[proto])
        for rep in range(1, n + 1):
            cfg = default_protocol_config(
                proto, rep, init_mode="paper",
                dataset_version=dataset_version,
                grader_version=grader_version)
            run_dir = root / f"experiments/{proto}/run{rep:02d}"
            run_dir.mkdir(parents=True, exist_ok=True)
            payload = cfg.to_dict()
            payload["dataset_version"] = dataset_version
            (run_dir / "protocol.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8")
            plan.append({"protocol": proto, "replicate": rep,
                         "dir": str(run_dir), "config": payload})
    (plan_root / "plan.json").write_text(
        json.dumps({"dataset_version": dataset_version, "runs": plan},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def estimate_budget(cfg: ProtocolConfig, train_tasks: int,
                    val_tasks: int, test_tasks: int = 0,
                    retry_margin: float = 0.15) -> dict:
    """按部件估算调用量；估算≠硬上限。无价格 → cost=None，不编造金额。

    部件：基线(一次 val 全程) + 训练内多次请求(逐轮×train) + 维护(逐轮) +
    提议多轮(逐轮×(action1+轨迹读取4)) + 候选验证(逐轮×val) +
    最终测试(冻结后 test 一次/每运行) + 重试余量(显式百分比，非硬上限)。
    """
    rounds = cfg.iterations if cfg.evolve else 0
    baseline_calls = val_tasks            # 首次基线验证（val 全程一次）
    train_calls = rounds * train_tasks
    maintain_calls = rounds if (cfg.evolve and cfg.experience != "none") else 0
    propose_calls = rounds * (1 + 4) if cfg.evolve else 0
    eval_calls = rounds * val_tasks
    final_test_calls = test_tasks         # 冻结后测试评估（每运行一次）
    subtotal = (baseline_calls + train_calls + maintain_calls +
                propose_calls + eval_calls + final_test_calls)
    margin = int(subtotal * retry_margin) if cfg.evolve else 0
    total = subtotal + margin
    return {
        "rounds": rounds,
        "parts": {
            "baseline": baseline_calls,
            "train": train_calls, "maintain": maintain_calls,
            "propose": propose_calls, "candidate_eval": eval_calls,
            "final_test": final_test_calls,
            "retry_margin(est)": margin,
        },
        "estimated_model_calls": total,
        "estimated_tool_calls": propose_calls if cfg.evolve else 0,
        "formula": "baseline=val; train=rounds*train; maintain=rounds(非C); "
                   "propose=rounds*(1+4); eval=rounds*val; final_test=test; "
                   "margin=15%估算",
        "model_calls_cap": int((cfg.budget or {}).get("max_model_calls") or 0),
        "unknown": ["token_usage", "cost", "真实失败/重试次数",
                    "逐任务真实请求数（多阶段编译内部请求数）"],
        "token_usage": None,       # 未知/不可得
        "cost": None,              # 无有效价格时不编造金额
        "model_ref": cfg.model_ref,
        "mode": cfg.model_mode,
    }


def freeze_skill_set(root: Path, run_dir: Path, skill_set: dict,
                     *, run_id: str | None = None,
                     experiment_id: str | None = None,
                     protocol: str | None = None,
                     replicate: int | None = None) -> Path:
    """冻结某次运行最终技能集合（不可覆盖）。含所属 run/experiment 与成员哈希。"""
    target = Path(run_dir) / FROZEN_SET_FILENAME
    if target.exists():
        raise FileExistsError(f"final skill set 已冻结，不可覆盖: {target}")
    members = list((skill_set or {}).get("members") or [])
    from app.core.skill_evolution.injector import EMPTY_SET_HASH, set_hash_for_members
    mode = (skill_set or {}).get("mode")
    if not members:
        set_hash = EMPTY_SET_HASH
        mode = mode or "empty"
        members_hash = EMPTY_SET_HASH
    else:
        set_hash = set_hash_for_members(members)
        members_hash = hashlib.sha256(
            canonical_json({"members": [
                {k: m.get(k) for k in ("skill_id", "version_id", "content_hash")}
                for m in members
            ]}).encode("utf-8")).hexdigest()
        mode = mode or "versions"
    if not run_id or not experiment_id or protocol is None or replicate is None:
        raise BatchError("冻结技能集缺少 run/experiment/protocol/replicate")
    payload = {
        "schema": FROZEN_SET_SCHEMA,
        "protocol": protocol,
        "replicate": replicate,
        "run_id": run_id,
        "experiment_id": experiment_id,
        "set_hash": set_hash,
        "members": members,
        "members_hash": members_hash,
        "mode": mode,
        "frozen_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    # 保留调用方额外字段（不含覆盖 schema 所有权）
    for k, v in (skill_set or {}).items():
        if k not in payload:
            payload[k] = v
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def ensure_all_frozen(plan_root: Path, runs: list[dict]) -> list[str]:
    """最终测试前：要求该批次所有待比较的最终技能集都已冻结（先全部冻结再测试）。

    返回未冻结的运行（protocol/replicate）。缺失 → 不允许任何一组的测试成绩先行消费。
    """
    missing = []
    for run in runs:
        if not (Path(run["dir"]) / "final-skill-set.json").is_file():
            missing.append(f"{run['protocol']}/run{run['replicate']:02d}")
    return missing


def mark_test_consumed(root: Path, dataset_version: str) -> Path:
    """测试结果用于调参时必须调用：记录该测试集已被消费。"""
    p = Path(root) / f"test-consumed-{dataset_version}.marker"
    p.write_text("consumed-by-tuning", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 配对 bootstrap（按来源组重采样；同组/同任务重复不当独立样本）
# ---------------------------------------------------------------------------


def paired_bootstrap(group_diffs: dict[str, list[float]], *,
                     n_boot: int = 2000, seed: int | None = None) -> dict:
    """输入 {group_id: [replicate差…]}；返回均值、95% CI、样本数、警告。

    组内多个 replicate 差先去平均成一个组样本（重复运行不冒充独立新样本）。
    """
    if not group_diffs:
        return {"mean": None, "ci": None, "groups": 0, "note": "no groups"}
    rng = random.Random(seed)
    group_means = {g: sum(v) / len(v) for g, v in group_diffs.items()}
    groups = sorted(group_means)
    stats = [group_means[g] for g in groups]
    mean = sum(stats) / len(stats)
    boots = []
    for _ in range(n_boot):
        samples = [group_means[rng.choice(groups)] for _ in groups]
        boots.append(sum(samples) / len(samples))
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot)]
    return {
        "mean": mean, "ci": [lo, hi], "groups": len(groups),
        "group_means": group_means,
        "note": ("按来源组重采样；组内重复运行已先平均。小样本/宽区间如实展示，"
                 "不强行宣布显著提升。")}


class BatchError(ValueError):
    """正式批次计划/执行/冻结校验失败。"""


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dataset_split_manifest(dataset: DatasetSpec) -> dict:
    splits = {"train": [], "val": [], "test": []}
    for t in dataset.tasks:
        splits[t.split].append({
            "task_id": t.task_id,
            "group_id": t.group_id,
            "content_hash": hashlib.sha256(
                canonical_json({
                    "task_id": t.task_id, "instruction": t.instruction,
                    "sources": [s.to_dict() for s in t.sources],
                }).encode("utf-8")).hexdigest(),
        })
    return splits


def validate_batch_inputs(dataset: DatasetSpec, cfgs: list[ProtocolConfig]) -> list[str]:
    errors: list[str] = []
    ds_json = Path(dataset.dataset_dir) / "dataset.json"
    if not ds_json.is_file():
        errors.append("dataset.json 缺失")
    for cfg in cfgs:
        errors.extend(check_plan(dataset, cfg))
    c_cfgs = [c for c in cfgs if c.protocol == PROTOCOL_C]
    d_cfgs = [c for c in cfgs if c.protocol == PROTOCOL_D]
    for src in (c_cfgs, d_cfgs):
        for i, a in enumerate(src):
            for b in src[i + 1:]:
                if comparable_protocol_fields(a) != comparable_protocol_fields(b):
                    errors.append(
                        f"{a.protocol} 组 replicate 配置不一致 "
                        f"({a.replicate} vs {b.replicate})")
    for c in c_cfgs:
        for d in d_cfgs:
            if comparable_protocol_fields(c) != comparable_protocol_fields(d):
                errors.append(
                    f"C/D 除 experience 外配置不一致 "
                    f"(C/{c.replicate} vs D/{d.replicate})")
    return errors


_SECRET_MARKERS = ("api_key", "apikey", "authorization", "secret", "password",
                   "bearer", "credential_value")


def _reject_secret_payload(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower().replace("-", "_")
            if any(m in lk for m in _SECRET_MARKERS) and k not in (
                    "credential_ref", "credential_env", "api_key_present"):
                raise BatchError(f"禁止在批次配置中保存密钥字段: {path}{k}")
            if isinstance(v, str) and v.lower().startswith("bearer "):
                raise BatchError("禁止在批次配置中保存 Authorization")
            _reject_secret_payload(v, path=f"{path}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_secret_payload(v, path=f"{path}{i}.")


def _collect_dataset_files(dataset: DatasetSpec) -> dict[str, str]:
    ds_dir = Path(dataset.dataset_dir).resolve()
    files = {}
    ds_json = ds_dir / "dataset.json"
    files["dataset.json"] = _sha256_path(ds_json)
    src_root = ds_dir / "sources"
    if src_root.is_dir():
        for p in sorted(src_root.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(ds_dir)).replace("\\", "/")
                files[rel] = _sha256_path(p)
    ref_root = ds_dir / "references"
    if ref_root.is_dir():
        for p in sorted(ref_root.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(ds_dir)).replace("\\", "/")
                files[rel] = _sha256_path(p)
    for t in dataset.tasks:
        for s in t.sources or ():
            fp = ds_dir / "sources" / s.file
            if fp.is_file():
                rel = f"sources/{s.file}".replace("\\", "/")
                files[rel] = _sha256_path(fp)
        refp = ds_dir / "references" / t.reference_ref
        if refp.is_file():
            rel = f"references/{t.reference_ref}".replace("\\", "/")
            files[rel] = _sha256_path(refp)
    return files


def _plan_hash(plan: dict) -> str:
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()


def _normalize_test_budget(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        raise BatchError("real 批次 test 缺少独立预算（max_model_calls/max_seconds）")
    _reject_secret_payload(raw)
    try:
        max_model = int(raw.get("max_model_calls"))
        max_seconds = int(raw.get("max_seconds"))
    except (TypeError, ValueError):
        raise BatchError("test 预算字段缺失或非法")
    try:
        max_tool = int(raw.get("max_tool_calls") or 0)
    except (TypeError, ValueError):
        raise BatchError("test 预算 max_tool_calls 非法")
    if max_model < 1 or max_seconds < 1:
        raise BatchError("test 预算 max_model_calls 与 max_seconds 必须 >= 1")
    return {
        "max_model_calls": max_model,
        "max_seconds": max_seconds,
        "max_tool_calls": max_tool,
    }


def test_confirm_fingerprint(*, budget: dict, dataset_version: str,
                             plan_hash: str, config_fingerprint) -> str:
    return hashlib.sha256(canonical_json({
        "budget": budget,
        "dataset_version": dataset_version,
        "plan_hash": plan_hash,
        "config_fingerprint": config_fingerprint,
    }).encode("utf-8")).hexdigest()


def _item_key(item: dict) -> str:
    return f"{item['protocol']}/{item['replicate']}"


def _test_state_path(root: Path) -> Path:
    return Path(root) / "plan" / TEST_STATE_FILENAME


def _test_state_seal_path(root: Path) -> Path:
    return Path(root) / "plan" / TEST_STATE_SEAL_FILENAME


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="bts.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_test_state_seal(root: Path) -> str | None:
    path = _test_state_seal_path(root)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text.removeprefix("sha256:") or None


def _load_test_state(root: Path) -> dict | None:
    path = _test_state_path(root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BatchError("batch-test 状态文件损坏")
    if not isinstance(data, dict):
        raise BatchError("batch-test 状态文件非法")
    return data


def _report_digest(row: dict) -> str:
    return hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest()


def _test_renew_interval(budget: dict) -> float:
    from app.core.skill_evolution.orchestrator import DEFAULT_LEASE_SECONDS
    budget_max = int((budget or {}).get("max_seconds") or 0)
    interval_base = budget_max if budget_max > 0 else DEFAULT_LEASE_SECONDS
    interval = min(5.0, max(1.0, interval_base / 4.0))
    lease_s = float(DEFAULT_LEASE_SECONDS)
    if interval >= lease_s:
        interval = max(0.2, lease_s / 4.0)
    return interval


def _bind_test_state_items(prepared: list) -> dict:
    items = {}
    for p in prepared:
        item = p["item"]
        frozen = p["frozen"] or {}
        key = _item_key(item)
        items[key] = {
            "protocol": item["protocol"],
            "replicate": item["replicate"],
            "frozen_set_hash": frozen.get("set_hash"),
            "frozen_members_hash": frozen.get("members_hash"),
        }
    return items


def _canonical_test_state(state: dict) -> tuple[dict, str, str]:
    payload = dict(state)
    payload["schema"] = TEST_STATE_SCHEMA
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return payload, text, digest


def _write_test_state_snapshot(root: Path, digest: str, text: str) -> Path:
    """写入不覆盖正式状态的唯一快照；文件名保持短前缀，避免 Windows MAX_PATH。"""
    plan = Path(root) / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    path = _test_state_snapshot_path(root, digest)
    _atomic_write_text(path, text)
    return path


def _test_state_snapshot_path(root: Path, digest: str) -> Path:
    return Path(root) / "plan" / f"btsnap-{digest[:16]}.json"


def _install_official_test_state(root: Path, text: str, digest: str) -> None:
    _atomic_write_text(_test_state_path(root), text)
    _atomic_write_text(_test_state_seal_path(root), f"sha256:{digest}\n")


def _merge_test_state_into_config(cfg: dict, payload: dict, digest: str) -> str:
    merged = dict(cfg) if isinstance(cfg, dict) else {}
    merged["test_state_sha256"] = digest
    merged["test_state"] = payload
    return json.dumps(merged, ensure_ascii=False, sort_keys=True)


def _authority_test_state_from_cfg(cfg: dict) -> tuple[dict, str] | None:
    if not isinstance(cfg, dict):
        return None
    payload = cfg.get("test_state")
    digest = cfg.get("test_state_sha256")
    if not isinstance(payload, dict) or not digest:
        return None
    _, text, live = _canonical_test_state(payload)
    if live != digest:
        return None
    return payload, digest


def _try_restore_official_from_cfg(root: Path, cfg: dict) -> dict | None:
    auth = _authority_test_state_from_cfg(cfg)
    if auth is None:
        return None
    payload, digest = auth
    _, text, _ = _canonical_test_state(payload)
    _write_test_state_snapshot(root, digest, text)
    _install_official_test_state(root, text, digest)
    return payload


def _repair_official_test_state_from_authority(
        root: Path, row, db, *, dataset, manifest: dict, status: dict,
        budget: dict, prepared: list) -> None:
    """用 DB 权威状态修复双文件镜像；修复后要求重试，当前调用不发请求。"""
    from app.core.skill_evolution import runenv
    from app.models.evolution import EvolutionExperiment

    try:
        cfg = json.loads(row.config_json or "{}")
    except ValueError as exc:
        raise BatchError("test 预算账本 config_json 损坏") from exc
    auth = _authority_test_state_from_cfg(cfg)
    if auth is None:
        raise BatchError("test 预算账本缺少有效权威状态")
    payload, digest = auth

    official = _test_state_path(root)
    seal = _read_test_state_seal(root)
    official_hash = _sha256_path(official) if official.is_file() else None
    if official_hash == digest and seal == digest:
        return

    for key in TEST_STATE_REQUIRED:
        if key not in payload:
            raise BatchError(f"DB 权威 batch-test 状态缺字段: {key}")
    if payload.get("schema") != TEST_STATE_SCHEMA:
        raise BatchError("DB 权威 batch-test 状态 schema 不匹配")
    if payload.get("run_id") != row.run_id or \
            payload.get("experiment_id") != row.experiment_id:
        raise BatchError("DB 权威 batch-test 状态 run/experiment 归属不匹配")
    if db.get(EvolutionExperiment, payload["experiment_id"]) is None:
        raise BatchError("DB 权威 batch-test experiment 不存在")
    expected_lab = runenv.ensure_experiment_root(Path(root) / TEST_BUDGET_LAB)
    if Path(payload.get("lab") or "").resolve() != expected_lab.resolve():
        raise BatchError("DB 权威 batch-test 状态 lab 与当前批次不匹配")

    plan_hash = manifest.get("plan_hash")
    cfg_fp = status.get("config_fingerprint") or manifest.get("config_fingerprint")
    test_fp = status.get("test_confirm_fingerprint") or (
        (status.get("fingerprints") or {}).get("test_confirm_fingerprint"))
    if payload.get("dataset_version") != dataset.dataset_version or \
            payload.get("manifest_plan_hash") != plan_hash or \
            payload.get("config_fingerprint") != cfg_fp or \
            payload.get("test_confirm_fingerprint") != test_fp:
        raise BatchError("DB 权威 batch-test 状态批次指纹不匹配")
    if _budget_limits_tuple(payload.get("budget")) != _budget_limits_tuple(budget):
        raise BatchError("DB 权威 batch-test 状态预算不匹配")

    expected_items = _bind_test_state_items(prepared)
    if payload.get("items") != expected_items:
        raise BatchError("DB 权威 batch-test 状态 frozen 集合不匹配")
    completed = list(payload.get("completed") or [])
    reports = payload.get("reports") or {}
    report_hashes = payload.get("report_hashes") or {}
    if not isinstance(reports, dict) or not isinstance(report_hashes, dict):
        raise BatchError("DB 权威 batch-test reports 非法")
    for key in completed:
        if key not in expected_items or not isinstance(reports.get(key), dict):
            raise BatchError("DB 权威 batch-test completed/report 非法")
        report = reports[key]
        if report_hashes.get(key) != _report_digest(report):
            raise BatchError("DB 权威 batch-test report hash 失配")
        expected = expected_items[key]
        if report.get("frozen_set_hash") != expected.get("frozen_set_hash") or \
                report.get("frozen_members_hash") != expected.get("frozen_members_hash"):
            raise BatchError("DB 权威 batch-test report 与 frozen 集合不匹配")

    snapshot = _test_state_snapshot_path(root, digest)
    if not snapshot.is_file() or _sha256_path(snapshot) != digest:
        raise BatchError("DB 权威 batch-test 内容寻址快照缺失或损坏")
    try:
        snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise BatchError("DB 权威 batch-test 内容寻址快照非法") from exc
    if _canonical_test_state(snapshot_payload)[2] != digest:
        raise BatchError("DB 权威 batch-test 内容寻址快照内容失配")

    _, text, _ = _canonical_test_state(payload)
    _install_official_test_state(root, text, digest)
    raise BatchError("batch-test 正式状态已从 DB 权威快照恢复；请重试")


def _bootstrap_test_state(root: Path, state: dict, db) -> str:
    """首次建账：run 仍为 queued、无租约时写入权威状态。"""
    from sqlalchemy import update
    from app.models.evolution import EvolutionRun, RUN_QUEUED

    payload, text, digest = _canonical_test_state(state)
    _write_test_state_snapshot(root, digest, text)
    row = db.get(EvolutionRun, payload["run_id"])
    if row is None:
        raise BatchError("test 预算 run 丢失，拒绝写入状态")
    try:
        cfg = json.loads(row.config_json or "{}")
    except ValueError:
        cfg = {}
    new_cfg = _merge_test_state_into_config(cfg, payload, digest)
    result = db.execute(
        update(EvolutionRun)
        .where(EvolutionRun.run_id == payload["run_id"],
               EvolutionRun.status == RUN_QUEUED)
        .values(config_json=new_cfg))
    if result.rowcount != 1:
        db.rollback()
        raise BatchError(
            f"batch-test 初始状态写入失败 rowcount={result.rowcount}")
    db.commit()
    _install_official_test_state(root, text, digest)
    return digest


def _commit_test_state_fenced(root: Path, state: dict, db, *,
                              token: str, expected_sha: str) -> str:
    """单条条件 UPDATE：租约 + 期望 previous hash。rowcount!=1 不得改正式文件。"""
    global last_test_state_cas_rowcount
    from sqlalchemy import text as sql_text
    from app.core.skill_evolution.orchestrator import CLOCK, LeaseConflict
    from app.models.evolution import EvolutionRun, RUN_RUNNING

    if not token:
        last_test_state_cas_rowcount = 0
        raise LeaseConflict("batch-test 状态 CAS 失败 rowcount=0：缺少 lease_token")
    if not expected_sha:
        last_test_state_cas_rowcount = 0
        raise LeaseConflict("batch-test 状态 CAS 失败 rowcount=0：缺少 expected hash")

    payload, body, digest = _canonical_test_state(state)
    _write_test_state_snapshot(root, digest, body)
    row = db.get(EvolutionRun, payload["run_id"])
    if row is None:
        last_test_state_cas_rowcount = 0
        raise BatchError("test 预算 run 丢失，拒绝写入状态")
    try:
        cfg = json.loads(row.config_json or "{}")
    except ValueError:
        cfg = {}
    new_cfg = _merge_test_state_into_config(cfg, payload, digest)
    now = CLOCK.now()
    result = db.execute(
        sql_text(
            "UPDATE evolution_runs SET config_json = :new_cfg "
            "WHERE run_id = :run_id AND status = :status "
            "AND lease_token = :token AND lease_owner = :owner "
            "AND lease_expires_at > :now "
            "AND json_extract(config_json, '$.test_state_sha256') = :expected_sha"
        ),
        {
            "new_cfg": new_cfg,
            "run_id": payload["run_id"],
            "status": RUN_RUNNING,
            "token": token,
            "owner": BATCH_TEST_LEASE_OWNER,
            "now": now,
            "expected_sha": expected_sha,
        },
    )
    rowcount = int(result.rowcount or 0)
    last_test_state_cas_rowcount = rowcount
    if rowcount != 1:
        db.rollback()
        raise LeaseConflict(
            f"batch-test 状态 CAS 失败 rowcount={rowcount}")
    db.commit()
    db.expire_all()
    _install_official_test_state(root, body, digest)
    return digest


def _refresh_used_fields(state: dict, db) -> dict:
    from app.models.evolution import EvolutionRun
    row = db.get(EvolutionRun, state["run_id"])
    if row is not None:
        state["used_model_calls"] = int(row.used_model_calls or 0)
        state["used_tool_calls"] = int(row.used_tool_calls or 0)
    return state


def _save_test_state(root: Path, state: dict, db=None) -> None:
    """仅用于建账引导。claim 之后必须走 _commit_test_state_fenced。"""
    from app.core.skill_evolution import skill_store
    own = db is None
    if own:
        db = skill_store.session_for(Path(state["lab"]))
    try:
        _bootstrap_test_state(root, state, db)
    finally:
        if own:
            db.close()


def _budget_limits_tuple(budget: dict | None) -> tuple:
    b = budget or {}
    return (int(b.get("max_model_calls") or 0),
            int(b.get("max_seconds") or 0),
            int(b.get("max_tool_calls") or 0))


def _verify_test_state(root: Path, state: dict, *, dataset, manifest: dict,
                       status: dict, budget: dict, prepared: list,
                       db=None) -> dict:
    """fail-closed：缺字段/seal/归属/账本/completed 任一失败即拒绝。"""
    from app.core.skill_evolution import runenv, skill_store
    from app.models.evolution import EvolutionExperiment, EvolutionRun

    if not isinstance(state, dict):
        raise BatchError("batch-test 状态非法")
    for key in TEST_STATE_REQUIRED:
        if key not in state:
            raise BatchError(f"batch-test 状态缺字段: {key}")
    if state.get("schema") != TEST_STATE_SCHEMA:
        raise BatchError("batch-test 状态 schema 不匹配")
    path = _test_state_path(root)
    live = _sha256_path(path)
    seal = _read_test_state_seal(root)
    if not seal or seal != live:
        raise BatchError("batch-test 状态完整性校验失败")
    expected_lab = runenv.ensure_experiment_root(Path(root) / TEST_BUDGET_LAB)
    if Path(state["lab"]).resolve() != Path(expected_lab).resolve():
        raise BatchError("batch-test 状态 lab 与当前批次不匹配")
    plan_hash = manifest.get("plan_hash")
    cfg_fp = status.get("config_fingerprint") or manifest.get("config_fingerprint")
    test_fp = status.get("test_confirm_fingerprint") or (
        (status.get("fingerprints") or {}).get("test_confirm_fingerprint"))
    if state.get("dataset_version") != dataset.dataset_version:
        raise BatchError("batch-test 状态 dataset_version 不匹配")
    if state.get("manifest_plan_hash") != plan_hash:
        raise BatchError("batch-test 状态 manifest_plan_hash 不匹配")
    if state.get("config_fingerprint") != cfg_fp:
        raise BatchError("batch-test 状态 config_fingerprint 不匹配")
    if test_fp and state.get("test_confirm_fingerprint") != test_fp:
        raise BatchError("batch-test 状态 test_confirm_fingerprint 不匹配")
    if _budget_limits_tuple(state.get("budget")) != _budget_limits_tuple(budget):
        raise BatchError("batch-test 状态预算上限与当前批次不一致")
    items = state.get("items") or {}
    expected_items = _bind_test_state_items(prepared)
    if set(items) != set(expected_items):
        raise BatchError("batch-test 状态 protocol/replicate 与当前冻结集不一致")
    for key, exp in expected_items.items():
        got = items.get(key) or {}
        if got.get("frozen_set_hash") != exp.get("frozen_set_hash") or \
                got.get("frozen_members_hash") != exp.get("frozen_members_hash"):
            raise BatchError("batch-test 状态 frozen hash 与当前冻结集不一致")
    completed = list(state.get("completed") or [])
    reports = state.get("reports") or {}
    hashes = state.get("report_hashes") or {}
    if not isinstance(reports, dict) or not isinstance(hashes, dict):
        raise BatchError("batch-test 状态 reports 非法")
    for key in completed:
        if key not in expected_items:
            raise BatchError("batch-test 状态 completed 含未知项")
        row = reports.get(key)
        if not isinstance(row, dict):
            raise BatchError("batch-test 状态 completed 缺少对应 report")
        digest = _report_digest(row)
        if hashes.get(key) != digest:
            raise BatchError("batch-test 状态 report hash 失配")
        exp = expected_items[key]
        if row.get("frozen_set_hash") != exp.get("frozen_set_hash") or \
                row.get("frozen_members_hash") != exp.get("frozen_members_hash"):
            raise BatchError("batch-test 状态 report 与 frozen set 不一致")
    own = db is None
    if own:
        db = skill_store.session_for(expected_lab)
    try:
        row = db.get(EvolutionRun, state["run_id"])
        if row is None:
            raise BatchError("test 预算 run 不存在，拒绝清零重建")
        exp_row = db.get(EvolutionExperiment, state["experiment_id"])
        if exp_row is None:
            raise BatchError("test experiment 不存在，拒绝清零重建")
        if row.experiment_id != state["experiment_id"]:
            raise BatchError("test 预算 run 与 experiment 绑定不一致")
        cfg = json.loads(row.config_json or "{}")
        stored_budget = cfg.get("budget") or {}
        if _budget_limits_tuple(stored_budget) != _budget_limits_tuple(budget):
            raise BatchError("test 预算上限与账本不一致（拒绝改写已消费计数）")
        db_hash = cfg.get("test_state_sha256")
        if not db_hash:
            raise BatchError("batch-test 状态与账本 seal 不一致")
        if db_hash != live:
            restored = _try_restore_official_from_cfg(root, cfg)
            if restored is None:
                raise BatchError("batch-test 状态与账本 seal 不一致")
            state = restored
            path = _test_state_path(root)
            live = _sha256_path(path)
            seal = _read_test_state_seal(root)
            if not seal or seal != live or live != db_hash:
                raise BatchError("从账本恢复 batch-test 状态失败")
            for key in TEST_STATE_REQUIRED:
                if key not in state:
                    raise BatchError(f"batch-test 状态缺字段: {key}")
            items = state.get("items") or {}
            if set(items) != set(expected_items):
                raise BatchError("batch-test 状态 protocol/replicate 与当前冻结集不一致")
            completed = list(state.get("completed") or [])
            reports = state.get("reports") or {}
            hashes = state.get("report_hashes") or {}
            for key in completed:
                row_rep = reports.get(key)
                if not isinstance(row_rep, dict) or hashes.get(key) != _report_digest(row_rep):
                    raise BatchError("batch-test 状态 report hash 失配")
        state["used_model_calls"] = int(row.used_model_calls or 0)
        state["used_tool_calls"] = int(row.used_tool_calls or 0)
    finally:
        if own:
            db.close()
    return state


def _uncertain_test_keys(root: Path, state: dict, prepared: list) -> set[str]:
    """无法安全证明已完成后的保守窗口（at-least-once，不是 exactly-once）。"""
    uncertain: set[str] = set()
    ip = state.get("in_progress")
    if isinstance(ip, dict) and ip.get("key"):
        uncertain.add(str(ip["key"]))
    elif isinstance(ip, str) and ip:
        uncertain.add(ip)
    completed = set(state.get("completed") or [])
    for p in prepared:
        item = p["item"]
        key = _item_key(item)
        if key in completed:
            continue
        test_lab = Path(root) / "test-eval" / \
            f"{item['protocol']}{int(item['replicate']):02d}"
        runs = test_lab / "runs"
        if runs.is_dir() and any(runs.iterdir()):
            uncertain.add(key)
    return uncertain


def _skills_from_frozen_item(item: dict, frozen: dict, lab: Path) -> FrozenSkillSet:
    skills = _skill_set_from_frozen(frozen)
    if skills.mode == "versions" and not skills.instruction_text:
        from app.core.skill_evolution import skill_store, gating as gate
        db = skill_store.session_for(lab)
        try:
            members = frozen.get("members") or []
            skills = gate.build_set_from_members(db, members)
        finally:
            db.close()
    return skills


def write_batch_manifest(root: Path, dataset: DatasetSpec, plan: list,
                         *, frozen: dict | None, model_mode: str,
                         test_budget: dict | None = None) -> dict:
    protocols = {}
    budgets = {}
    for item in plan:
        pdir = Path(item["dir"])
        proto_path = pdir / "protocol.json"
        protocols[f"{item['protocol']}/{item['replicate']}"] = _sha256_path(proto_path)
        cfg = item.get("config") or json.loads(proto_path.read_text(encoding="utf-8"))
        budgets[f"{item['protocol']}/{item['replicate']}"] = cfg.get("budget")
    seed = seed_package_fingerprints() if any(
        (item.get("config") or {}).get("protocol") == PROTOCOL_B
        or item.get("protocol") == PROTOCOL_B for item in plan) else None
    actor_fp = None
    if frozen:
        from app.core.skill_evolution import config_freeze
        actor_fp = config_freeze.fingerprint_of(frozen)
    body = {
        "schema": "experiment-batch-manifest/v1",
        "dataset_version": dataset.dataset_version,
        "grader_version": dataset.grader_version,
        "model_mode": model_mode,
        "files": _collect_dataset_files(dataset),
        "splits": dataset_split_manifest(dataset),
        "protocols": protocols,
        "budgets": budgets,
        "seed": seed,
        "reviewer": (frozen or {}).get("frozen_reviewer"),
        "actor_fingerprint": actor_fp,
        "config_fingerprint": actor_fp,
        "plan_hash": _plan_hash({"dataset_version": dataset.dataset_version,
                                 "runs": plan, "model_mode": model_mode}),
        "test_budget": test_budget,
    }
    path = Path(root) / "plan" / "batch-manifest.json"
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    return body


def verify_batch_manifest(root: Path, dataset: DatasetSpec | None = None) -> dict:
    path = Path(root) / "plan" / "batch-manifest.json"
    if not path.is_file():
        raise BatchError("批次 manifest 缺失")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchError("批次 manifest 损坏") from exc
    plan = load_batch_plan(root)
    ds = dataset
    if ds is None:
        # 仅校验计划与 protocol 文件哈希时允许不传 dataset；run/test 必须传入。
        expected_plan = _plan_hash({
            "dataset_version": plan.get("dataset_version"),
            "runs": plan.get("runs"),
            "model_mode": plan.get("model_mode") or stored.get("model_mode"),
        })
        if stored.get("plan_hash") != expected_plan:
            raise BatchError("批次 plan 与 manifest plan_hash 不一致")
        for item in plan.get("runs") or []:
            key = f"{item['protocol']}/{item['replicate']}"
            proto = Path(item["dir"]) / "protocol.json"
            if not proto.is_file():
                raise BatchError(f"protocol.json 缺失: {key}")
            if stored.get("protocols", {}).get(key) != _sha256_path(proto):
                raise BatchError(f"protocol.json 已被修改: {key}")
            live = json.loads(proto.read_text(encoding="utf-8"))
            if live.get("budget") != stored.get("budgets", {}).get(key):
                raise BatchError(f"protocol budget 已被修改: {key}")
        return stored
    if ds.dataset_version != stored.get("dataset_version"):
        raise BatchError("dataset_version 与 manifest 不一致")
    live_files = _collect_dataset_files(ds)
    if live_files != stored.get("files"):
        raise BatchError("数据集或 reference/source 字节已变化（即使 dataset_version 相同）")
    if dataset_split_manifest(ds) != stored.get("splits"):
        raise BatchError("train/val/test 清单与 manifest 不一致")
    expected_plan = _plan_hash({
        "dataset_version": plan.get("dataset_version"),
        "runs": plan.get("runs"),
        "model_mode": plan.get("model_mode") or stored.get("model_mode"),
    })
    if stored.get("plan_hash") != expected_plan:
        raise BatchError("批次 plan 与 manifest plan_hash 不一致")
    for item in plan.get("runs") or []:
        key = f"{item['protocol']}/{item['replicate']}"
        proto = Path(item["dir"]) / "protocol.json"
        if stored.get("protocols", {}).get(key) != _sha256_path(proto):
            raise BatchError(f"protocol.json 已被修改: {key}")
        live = json.loads(proto.read_text(encoding="utf-8"))
        if live.get("budget") != stored.get("budgets", {}).get(key):
            raise BatchError(f"protocol budget 已被修改: {key}")
    return stored


def create_batch(root: Path, dataset: DatasetSpec, *,
                 runs: dict[str, int] | None = None,
                 model_mode: str = "simulated",
                 iterations: int | None = None,
                 real_config: dict | None = None,
                 group_budgets: dict | None = None,
                 group_overrides: dict | None = None,
                 test_budget: dict | None = None) -> dict:
    """创建正式四组批次：校验数据集/评分器/种子/隔离路径，写入 plan + manifest。"""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if model_mode not in ("simulated", "real"):
        raise BatchError(f"model_mode 非法: {model_mode}")
    frozen = None
    test_budget_doc = None
    review = "v2" if "v2" in (dataset.grader_version or "") else "v1"
    if model_mode == "real":
        if not real_config:
            raise BatchError("real 模式缺少非秘密配置（--real-config）")
        _reject_secret_payload(real_config)
        if not real_config.get("provider") or not real_config.get("credential_ref"):
            raise BatchError("real 配置必须包含 provider 与 credential_ref")
        if not real_config.get("model"):
            raise BatchError("real 模式需要显式 model_ref（不能猜测供应商 model ID）")
        from app.core.skill_evolution import config_freeze
        from app.core.skill_evolution.credential_binding import (
            CredentialBindingError, provider_fields)
        try:
            cred_env, _endpoints, _https = provider_fields(real_config["provider"])
        except CredentialBindingError as exc:
            raise BatchError(f"real provider 绑定不可用: {exc}") from exc
        if cred_env != str(real_config.get("credential_ref")):
            raise BatchError("credential_ref 与 provider 绑定不一致")
        override = {}
        if real_config.get("api_url"):
            override["llm_api_url"] = real_config["api_url"]
        override["provider"] = real_config["provider"]
        for k in ("model", "timeout", "retries", "max_output_tokens"):
            if k in real_config and real_config[k] is not None:
                override[k] = real_config[k]
        try:
            frozen = config_freeze.build_frozen_block(
                override=override, review=review if review == "v2" else None)
        except config_freeze.FrozenConfigError as exc:
            raise BatchError(f"real 配置无法冻结: {exc}") from exc
        test_budget_doc = _normalize_test_budget(test_budget)
    elif test_budget:
        test_budget_doc = _normalize_test_budget(test_budget)
    plan = build_plan(root, dataset.dataset_version, runs, dataset=dataset)
    for p in plan:
        cfg_path = Path(p["dir"]) / "protocol.json"
        payload = json.loads(cfg_path.read_text(encoding="utf-8"))
        if model_mode:
            p["config"]["model_mode"] = model_mode
            payload["model_mode"] = model_mode
        if model_mode == "real":
            payload["model_ref"] = real_config.get("model")
            p["config"]["model_ref"] = real_config.get("model")
            payload["frozen"] = frozen
            p["config"]["frozen"] = frozen
            payload["real_config"] = {
                "provider": real_config.get("provider"),
                "credential_ref": real_config.get("credential_ref"),
                "model": real_config.get("model"),
                "api_url": real_config.get("api_url"),
                "timeout": real_config.get("timeout"),
                "retries": real_config.get("retries"),
                "max_output_tokens": real_config.get("max_output_tokens"),
            }
            p["config"]["real_config"] = payload["real_config"]
            _reject_secret_payload(payload["real_config"])
        if iterations is not None and p["config"].get("evolve"):
            p["config"]["iterations"] = int(iterations)
            payload["iterations"] = int(iterations)
            budget = dict(payload.get("budget") or p["config"].get("budget") or {})
            budget["max_iterations"] = int(iterations)
            payload["budget"] = budget
            p["config"]["budget"] = budget
        proto = p["protocol"]
        if group_budgets and proto in group_budgets:
            budget = dict(payload.get("budget") or {})
            budget.update(group_budgets[proto])
            payload["budget"] = budget
            p["config"]["budget"] = budget
        if group_overrides and proto in group_overrides:
            by_rep = group_overrides[proto]
            extra = by_rep.get(p["replicate"]) if isinstance(by_rep, dict) else None
            if extra:
                payload.update(extra)
                p["config"].update(extra)
        cfg_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        p["config"] = payload
    cfgs = [ProtocolConfig.from_dict(p["config"]) for p in plan]
    plan_doc = {"dataset_version": dataset.dataset_version, "runs": plan,
                "model_mode": model_mode}
    (root / "plan" / "plan.json").write_text(
        json.dumps(plan_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    errors = validate_batch_inputs(dataset, cfgs)
    if errors:
        raise BatchError("批次计划校验失败: " + "; ".join(errors))
    manifest = write_batch_manifest(
        root, dataset, plan, frozen=frozen, model_mode=model_mode,
        test_budget=test_budget_doc)
    ds_dir = Path(dataset.dataset_dir)
    fingerprints = {
        "dataset_version": dataset.dataset_version,
        "dataset_json": _sha256_path(ds_dir / "dataset.json"),
        "grader_version": dataset.grader_version,
        "splits": dataset_split_manifest(dataset),
        "seed": seed_package_fingerprints() if any(
            c.protocol == PROTOCOL_B for c in cfgs) else None,
        "manifest_plan_hash": manifest["plan_hash"],
        "actor_fingerprint": manifest.get("actor_fingerprint"),
    }
    test_fp = None
    if test_budget_doc:
        test_fp = test_confirm_fingerprint(
            budget=test_budget_doc,
            dataset_version=dataset.dataset_version,
            plan_hash=manifest["plan_hash"],
            config_fingerprint=manifest.get("config_fingerprint"))
        fingerprints["test_confirm_fingerprint"] = test_fp
    status = {
        "schema": "experiment-batch/v1",
        "dataset_version": dataset.dataset_version,
        "grader_version": dataset.grader_version,
        "fingerprints": fingerprints,
        "runs": plan,
        "created": True,
        "model_mode": model_mode,
        "frozen": frozen,
        "config_fingerprint": manifest.get("config_fingerprint"),
        "test_budget": test_budget_doc,
        "test_confirm_fingerprint": test_fp,
    }
    (root / "plan" / "batch-status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return status


def load_batch_plan(root: Path) -> dict:
    plan_file = Path(root) / "plan" / "plan.json"
    if not plan_file.is_file():
        raise BatchError("批次计划不存在：先 experiment-batch-create")
    return json.loads(plan_file.read_text(encoding="utf-8"))


def load_frozen_set(run_dir: Path) -> dict:
    path = Path(run_dir) / FROZEN_SET_FILENAME
    if not path.is_file():
        raise BatchError(f"冻结技能集缺失: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BatchError(f"冻结技能集损坏: {path}") from exc
    if not isinstance(data, dict):
        raise BatchError(f"冻结技能集不是对象: {path}")
    return data


_FROZEN_REQUIRED = (
    "schema", "protocol", "replicate", "run_id", "experiment_id",
    "mode", "set_hash", "members", "members_hash", "frozen_at",
)


def verify_frozen_set(run_dir: Path, *, run_id: str | None,
                      experiment_id: str | None,
                      protocol: str | None = None,
                      replicate: int | None = None,
                      lab: Path | None = None) -> dict:
    data = load_frozen_set(run_dir)
    for field in _FROZEN_REQUIRED:
        if field not in data or data.get(field) is None:
            raise BatchError(f"冻结技能集缺少必填字段: {field}")
    if data.get("schema") != FROZEN_SET_SCHEMA:
        raise BatchError("冻结技能集 schema 必须为 frozen-skill-set/v1")
    if protocol is not None and data.get("protocol") != protocol:
        raise BatchError("冻结技能集 protocol 与计划不一致")
    if replicate is not None and int(data.get("replicate")) != int(replicate):
        raise BatchError("冻结技能集 replicate 与计划不一致")
    if not run_id or data.get("run_id") != run_id:
        raise BatchError("冻结技能集不属于该 run")
    if not experiment_id or data.get("experiment_id") != experiment_id:
        raise BatchError("冻结技能集不属于该 experiment")
    members = data.get("members")
    if not isinstance(members, list):
        raise BatchError("冻结技能集 members 必须是列表")
    from app.core.skill_evolution.injector import EMPTY_SET_HASH, set_hash_for_members
    expected_members_hash = hashlib.sha256(
        canonical_json({"members": [
            {k: m.get(k) for k in ("skill_id", "version_id", "content_hash")}
            for m in members
        ]}).encode("utf-8")).hexdigest() if members else EMPTY_SET_HASH
    if data.get("members_hash") != expected_members_hash:
        raise BatchError("冻结技能集 members_hash 不匹配")
    if not members:
        if data.get("set_hash") != EMPTY_SET_HASH:
            raise BatchError("空集合 set_hash 必须为 EMPTY_SET_HASH")
    else:
        expected_set = set_hash_for_members(members)
        if data.get("set_hash") != expected_set:
            raise BatchError("冻结技能集 set_hash 与 FrozenSkillSet 算法不一致")
    if lab is not None and members:
        from app.core.skill_evolution import skill_store
        db = skill_store.session_for(lab)
        try:
            for m in members:
                try:
                    v = skill_store.get_version(db, m.get("version_id"))
                except Exception as exc:  # noqa: BLE001
                    raise BatchError(
                        f"冻结成员 version 无法读取: {m.get('version_id')}") from exc
                if v is None:
                    raise BatchError(f"冻结成员 version 不存在: {m.get('version_id')}")
                live_hash = skill_store._recompute_hash(
                    v.version_id, v.skill_id, v.domain, v.runtime_ref,
                    v.schema_version, v.skill_md or "", v.purpose_md or "")
                if (v.skill_id != m.get("skill_id")
                        or v.version_id != m.get("version_id")
                        or v.content_hash != m.get("content_hash")
                        or live_hash != m.get("content_hash")
                        or int(v.seq or 0) != int(m.get("seq") or 0)):
                    raise BatchError("冻结成员与数据库不可变版本不一致")
                if live_hash != m.get("content_hash"):
                    raise BatchError("数据库版本内容哈希与冻结声明不符")
        finally:
            db.close()
    return data


def _skill_set_from_frozen(data: dict) -> FrozenSkillSet:
    members = data.get("members") or []
    if not members or data.get("mode") in (None, "none", "empty"):
        if data.get("mode") == "none":
            return FrozenSkillSet.disabled()
        return FrozenSkillSet.empty()
    from types import SimpleNamespace
    rows = []
    for m in members:
        rows.append(SimpleNamespace(
            skill_id=m["skill_id"], version_id=m["version_id"],
            content_hash=m["content_hash"], seq=int(m.get("seq") or 0),
            skill_md=m.get("skill_md") or ""))
    if any(not getattr(r, "skill_md", "") for r in rows):
        # 冻结文件可能不含正文：按 version_id 无法在此 DB 外重建时保持 empty+hash
        return FrozenSkillSet(
            mode="versions",
            version_ids=tuple(m["version_id"] for m in members),
            content_hashes=tuple(m["content_hash"] for m in members),
            set_hash=data.get("set_hash"),
            instruction_text=None,
            members=tuple(members),
        )
    return FrozenSkillSet.from_versions(rows)


def evaluate_frozen_test(root: Path, dataset: DatasetSpec,
                         skill_set: FrozenSkillSet, profile: str = "faithful",
                         executor_runner: Callable | None = None,
                         reviewer=None) -> dict:
    """冻结后独立测试评估：只跑 test split，不进入任何训练/门控回路。"""
    use_v2 = dataset.grader_version != "wiki-default-grader/v1" and \
        "v2" in (dataset.grader_version or "")
    if use_v2 and reviewer is None:
        raise BatchError("v2 grader 测试评估缺少 reviewer（不回退 v1）")
    results = []
    for t in dataset.tasks:
        if t.split != "test":
            continue
        res = run_one(root, dataset, t, profile=profile, skills=skill_set,
                      llm_runner_override=executor_runner)
        meta = res["meta"]
        if use_v2:
            from app.core.skill_evolution import review_eval
            grading = review_eval.grade_task_v2(
                dataset_dir=dataset.dataset_dir, task=t,
                candidate=res["candidate"], reviewer=reviewer)
            checks = (int(grading.get("checks_passed") or 0),
                      int(grading.get("checks_total") or 0))
        else:
            grading = grade_task(outcome=meta["outcome"], candidate=res["candidate"],
                                 reference_ref=t.reference_ref,
                                 dataset_dir=dataset.dataset_dir, task_id=t.task_id)
            checks = (sum(1 for r in grading["item_results"] if r["passed"]),
                      len(grading["item_results"]))
        results.append({
            "task_id": t.task_id, "group_id": t.group_id,
            "execution_id": res["execution_id"],
            "verdict": grading["verdict"],
            "checks": checks,
        })
    passed = sum(1 for r in results if r["verdict"] == "pass")
    return {"passed": passed, "total": len(results),
            "protocol": "passed_over_total_v1", "per_task": results}


def _replicate_root(batch_root: Path, protocol: str, replicate: int) -> Path:
    return Path(batch_root) / f"experiments/{protocol}/run{replicate:02d}" / "lab"


def _write_run_state(run_dir: Path, state: dict) -> None:
    (Path(run_dir) / "run-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_run_state(run_dir: Path) -> dict | None:
    p = Path(run_dir) / "run-state.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _role_counter():
    counts = {"executor": 0, "maintainer": 0, "proposer": 0, "reviewer": 0}

    def wrap(role, inner):
        def _fn(messages, context="", timeout=120.0):
            counts[role] = counts.get(role, 0) + 1
            if inner is None:
                raise RuntimeError(f"{role} runner 被调用但未配置")
            return inner(messages, context=context, timeout=timeout)
        return _fn
    return counts, wrap


def _run_one_replicate(batch_root: Path, dataset: DatasetSpec, item: dict, *,
                       resume: bool = False, executor_runner=None,
                       maintainer_runner=None, proposer_factory=None,
                       reviewer_factory=None, simulated_forbidden: bool = False) -> dict:
    from app.core.skill_evolution import (
        gating as gate,
        orchestrator as orch,
        runenv,
        skill_store,
        experience_store as exp_store,
    )
    from app.core.skill_evolution.orchestrator import Actors, PolicyProposer, create_run, execute
    from app.core.skill_evolution.trace_sampling import group_workspace_id

    cfg = ProtocolConfig.from_dict(item["config"])
    run_dir = Path(item["dir"])
    lab = runenv.ensure_experiment_root(_replicate_root(batch_root, cfg.protocol, cfg.replicate))
    sim_prev = None
    if simulated_forbidden:
        from app.core.skill_evolution.runner import SimulatedModel
        sim_prev = SimulatedModel.__call__
        def _boom(*_a, **_k):
            raise RuntimeError("SimulatedModel 被调用：real-stub 路径不得静默回退")
        SimulatedModel.__call__ = _boom  # type: ignore[method-assign]

    db = skill_store.session_for(lab)
    try:
        state = _read_run_state(run_dir) if resume else None
        if state and state.get("run_id"):
            run_id = state["run_id"]
        else:
            seed_id = None
            if cfg.protocol == PROTOCOL_B or cfg.init_mode == "seed":
                pkg = skill_store.load_package(SEED_DEFAULT_DIR, source_type="builtin_seed")
                seed_id = skill_store.import_seed(db, pkg)
                v = skill_store.get_version(db, seed_id)
                if cfg.seed_content_hash and v.content_hash != cfg.seed_content_hash:
                    raise BatchError("B 组导入种子 content_hash 与计划不一致")
                initial_members = [{"skill_id": v.skill_id, "version_id": v.version_id,
                                    "content_hash": v.content_hash, "seq": v.seq}]
            else:
                initial_members = []
            train_ids = [t.task_id for t in dataset.tasks if t.split == "train"]
            val_ids = [t.task_id for t in dataset.tasks if t.split == "val"]
            ws = group_workspace_id(
                next(t.group_id for t in dataset.tasks if t.split == "train"))
            review = "v2" if "v2" in cfg.grader_version else "v1"
            frozen_block = (item.get("config") or {}).get("frozen")
            rconfig = {"profile": "faithful", "review": review}
            if cfg.model_mode == "real":
                if not frozen_block:
                    raise BatchError("real 计划缺少冻结配置")
                rconfig.update({
                    "mode": "real", "real": True, "freeze_eligible": True,
                    "frozen": frozen_block,
                    "override": (item.get("config") or {}).get("real_config") or {},
                })
            exp_info = gate.create_experiment(
                db, workspace_id=ws, domain=dataset.domain, dataset=dataset,
                grader_version=dataset.grader_version,
                runner_config={"profile": "faithful", "review": review,
                               "val_task_ids": val_ids},
                pipeline_key="wiki.default", pipeline_version="3",
                runtime_ref=skill_store.RUNTIME_REF,
                val_task_ids=val_ids, initial_members=initial_members)
            run = create_run(
                db, experiment_id=exp_info.experiment_id, workspace_id=ws,
                domain=dataset.domain, dataset=dataset,
                init_mode="paper" if cfg.init_mode == "empty" else "business",
                max_iterations=max(1, cfg.iterations),
                budget=dict(cfg.budget),
                runner_config=rconfig,
                train_task_ids=train_ids or [t.task_id for t in dataset.tasks if t.split == "val"][:1],
                experience=cfg.experience if cfg.protocol in (PROTOCOL_C, PROTOCOL_D) else "none",
                evolve=cfg.evolve)
            run_id = run.run_id
            _write_run_state(run_dir, {
                "run_id": run_id, "experiment_id": exp_info.experiment_id,
                "lab": str(lab), "protocol": cfg.protocol, "replicate": cfg.replicate,
            })
    finally:
        db.close()

    counts, wrap = _role_counter()
    if cfg.model_mode == "real":
        from app.core.skill_evolution import control
        try:
            built, used_mode = control.build_run_actors(lab, run_id)
        except control.ControlError as exc:
            raise BatchError(f"real 批次无法构造授权 actor: {exc}") from exc
        if used_mode != "real":
            raise BatchError("real 批次不得回退模拟 actor")
        actors = Actors(
            execution_profile="faithful",
            executor_runner=wrap("executor", built.executor_runner),
            maintainer_runner=(wrap("maintainer", built.maintainer_runner)
                               if built.maintainer_runner else None),
            proposer_factory=((lambda: wrap("proposer", built.proposer_factory()))
                              if built.proposer_factory else None),
            reviewer_factory=built.reviewer_factory,
        )
    else:
        exec_inner = executor_runner
        actors = Actors(
            execution_profile="faithful",
            executor_runner=wrap("executor", exec_inner) if exec_inner else None,
            maintainer_runner=wrap("maintainer", maintainer_runner) if maintainer_runner else None,
            proposer_factory=((lambda: wrap("proposer", proposer_factory()))
                              if proposer_factory else None),
            reviewer_factory=reviewer_factory,
        )
        if cfg.evolve and proposer_factory is None and exec_inner is None:
            actors.proposer_factory = None  # execute 内用 PolicyProposer
    try:
        view = execute(lab, dataset, run_id, resume=resume, actors=actors)
        db = skill_store.session_for(lab)
        try:
            exp_info = gate.get_experiment(db, view["experiment_id"])
            current = exp_info.current_skill_set or {}
            n_patterns = len(exp_store.list_patterns(
                db, view["workspace_id"], dataset.domain))
            version_ids = [m["version_id"] for m in (current.get("members") or [])]
            if view.get("status") == "completed":
                freeze_skill_set(
                    lab, run_dir, current, run_id=run_id,
                    experiment_id=view["experiment_id"],
                    protocol=cfg.protocol, replicate=cfg.replicate)
        finally:
            db.close()
        role_calls = {
            "maintainer": sum(1 for it in view.get("iterations") or []
                              if it.get("maintenance_run_id")),
            "proposer": sum(1 for it in view.get("iterations") or []
                            if it.get("proposal_id") or it.get("proposal_run_id")),
            "iterations": len(view.get("iterations") or []),
        }
        if view.get("status") == "completed" and not cfg.evolve:
            if role_calls["maintainer"] or role_calls["proposer"] or role_calls["iterations"]:
                raise BatchError(
                    f"{cfg.protocol} 组出现了演化步骤（Maintainer/Proposer/iteration）")
            if cfg.protocol == PROTOCOL_A and version_ids:
                raise BatchError("A 组冻结后仍有技能版本")
        return {
            "protocol": cfg.protocol, "replicate": cfg.replicate,
            "run_id": run_id, "status": view.get("status"),
            "stop_reason": view.get("stop_reason"),
            "used": view.get("used"),
            "role_calls": role_calls,
            "pattern_count": n_patterns,
            "skill_versions": version_ids,
            "lab": str(lab),
            "dir": str(run_dir),
            "experiment_id": view.get("experiment_id"),
            "counts_runtime": counts,
            "frozen": view.get("status") == "completed",
        }
    finally:
        if sim_prev is not None:
            from app.core.skill_evolution.runner import SimulatedModel
            SimulatedModel.__call__ = sim_prev  # type: ignore[method-assign]


def run_batch(root: Path, dataset: DatasetSpec, *, resume: bool = False,
              only: tuple[str, ...] | None = None,
              executor_runner=None, maintainer_runner=None,
              proposer_factory=None, reviewer_factory=None,
              simulated_forbidden: bool = False,
              confirm: dict | None = None) -> dict:
    verify_batch_manifest(Path(root), dataset)
    plan = load_batch_plan(root)
    model_mode = plan.get("model_mode") or "simulated"
    if not model_mode:
        runs = plan.get("runs") or []
        if runs:
            model_mode = (runs[0].get("config") or {}).get("model_mode") or "simulated"
    if model_mode == "real":
        if executor_runner is not None or maintainer_runner is not None or \
                proposer_factory is not None or reviewer_factory is not None:
            raise BatchError("real 批次不得传入临时 runner 绕过冻结配置")
        status = json.loads(
            (Path(root) / "plan" / "batch-status.json").read_text(encoding="utf-8"))
        expected_fp = status.get("config_fingerprint")
        confirm = confirm or {}
        got = confirm.get("config_fingerprint")
        if not got or got != expected_fp:
            raise BatchError("real 批次需要确认冻结 config_fingerprint")
        if confirm.get("dataset_version") != dataset.dataset_version:
            raise BatchError("real 批次数据集确认不匹配")
        if confirm.get("budget_ok") is not True:
            raise BatchError("real 批次预算未确认")
    results = []
    for item in plan["runs"]:
        if only and item["protocol"] not in only:
            continue
        state = _read_run_state(Path(item["dir"]))
        frozen = (Path(item["dir"]) / FROZEN_SET_FILENAME).is_file()
        if frozen:
            results.append({"protocol": item["protocol"],
                            "replicate": item["replicate"],
                            "skipped": "already_frozen",
                            "run_id": (state or {}).get("run_id")})
            continue
        results.append(_run_one_replicate(
            Path(root), dataset, item, resume=resume and bool(state),
            executor_runner=executor_runner,
            maintainer_runner=maintainer_runner,
            proposer_factory=proposer_factory,
            reviewer_factory=reviewer_factory,
            simulated_forbidden=simulated_forbidden or model_mode == "real"))
    return {"dataset_version": plan["dataset_version"], "results": results}


def resume_batch(root: Path, dataset: DatasetSpec, **kwargs) -> dict:
    return run_batch(root, dataset, resume=True, **kwargs)


def batch_status_metadata(root: Path) -> dict:
    """仅核验计划/protocol 文件哈希与冻结文件是否存在，不加载数据集。

    不构成完整校验：source/reference/dataset.json 篡改不会被发现。
    正式 CLI 不得调用本函数。
    """
    verify_batch_manifest(Path(root), None)
    plan = load_batch_plan(root)
    rows = []
    for item in plan["runs"]:
        d = Path(item["dir"])
        state = _read_run_state(d)
        frozen = (d / FROZEN_SET_FILENAME).is_file()
        rows.append({
            "protocol": item["protocol"], "replicate": item["replicate"],
            "dir": str(d), "run_id": (state or {}).get("run_id"),
            "frozen": frozen,
        })
    missing = ensure_all_frozen(Path(root), plan["runs"])
    return {"dataset_version": plan["dataset_version"], "runs": rows,
            "all_frozen": not missing, "missing_frozen": missing,
            "verification": "metadata-only"}


def batch_status(root: Path, dataset: DatasetSpec) -> dict:
    """完整核验：加载数据集并 verify_batch_manifest(root, dataset)。"""
    if dataset is None:
        raise BatchError("batch_status 需要 dataset（完整校验）；"
                         "内部 metadata-only 请用 batch_status_metadata")
    verify_batch_manifest(Path(root), dataset)
    plan = load_batch_plan(root)
    rows = []
    for item in plan["runs"]:
        d = Path(item["dir"])
        state = _read_run_state(d)
        frozen = (d / FROZEN_SET_FILENAME).is_file()
        rows.append({
            "protocol": item["protocol"], "replicate": item["replicate"],
            "dir": str(d), "run_id": (state or {}).get("run_id"),
            "frozen": frozen,
        })
    missing = ensure_all_frozen(Path(root), plan["runs"])
    out = {"dataset_version": plan["dataset_version"], "runs": rows,
           "all_frozen": not missing, "missing_frozen": missing}
    if _test_state_path(root).is_file():
        status_path = Path(root) / "plan" / "batch-status.json"
        status = {}
        if status_path.is_file():
            status = json.loads(status_path.read_text(encoding="utf-8"))
        manifest = verify_batch_manifest(Path(root), dataset)
        prepared = []
        for item in plan["runs"]:
            rst = _read_run_state(Path(item["dir"])) or {}
            lab = Path(rst.get("lab") or _replicate_root(
                root, item["protocol"], item["replicate"]))
            frozen = verify_frozen_set(
                Path(item["dir"]),
                run_id=rst.get("run_id"),
                experiment_id=rst.get("experiment_id"),
                protocol=item["protocol"],
                replicate=item["replicate"],
                lab=lab)
            prepared.append({"item": item, "frozen": frozen})
        budget = status.get("test_budget") or manifest.get("test_budget") or {}
        state = _load_test_state(root)
        if state is None:
            raise BatchError("batch-test 状态文件损坏")
        _verify_test_state(
            Path(root), state, dataset=dataset, manifest=manifest,
            status=status, budget=budget, prepared=prepared)
        out["test"] = {
            "completed": list(state.get("completed") or []),
            "used_model_calls": int(state.get("used_model_calls") or 0),
            "reports": dict(state.get("reports") or {}),
        }
    return out


def _require_real_test_confirm(dataset: DatasetSpec, manifest: dict,
                               status: dict, confirm: dict | None) -> dict:
    confirm = confirm or {}
    budget = status.get("test_budget") or manifest.get("test_budget")
    if not budget:
        raise BatchError("real 批次 test 缺少独立预算")
    plan_hash = manifest.get("plan_hash")
    cfg_fp = status.get("config_fingerprint") or manifest.get("config_fingerprint")
    expected = test_confirm_fingerprint(
        budget=budget, dataset_version=dataset.dataset_version,
        plan_hash=plan_hash, config_fingerprint=cfg_fp)
    stored_fp = status.get("test_confirm_fingerprint") or (
        (status.get("fingerprints") or {}).get("test_confirm_fingerprint"))
    if stored_fp and stored_fp != expected:
        raise BatchError("test 确认指纹已漂移")
    got = confirm.get("test_fingerprint") or confirm.get("test_confirm_fingerprint")
    if not got or got != expected:
        raise BatchError("real 批次 test 需要确认完整 test 指纹")
    if not confirm.get("dataset_version"):
        raise BatchError("real 批次 test 缺少 dataset_version 确认")
    if confirm.get("dataset_version") != dataset.dataset_version:
        raise BatchError("real 批次 test 数据集确认不匹配")
    if not confirm.get("plan_hash"):
        raise BatchError("real 批次 test 缺少 manifest plan_hash 确认")
    if confirm.get("plan_hash") != plan_hash:
        raise BatchError("real 批次 test manifest plan_hash 确认不匹配")
    if confirm.get("budget_ok") is not True:
        raise BatchError("real 批次 test 预算未确认")
    if confirm.get("config_fingerprint") and confirm.get("config_fingerprint") != cfg_fp:
        raise BatchError("real 批次 test 配置指纹确认不匹配")
    if confirm.get("budget") is not None and confirm.get("budget") != budget:
        raise BatchError("test 预算已漂移")
    return budget


def _bind_test_reviewer(reviewer, guard):
    if reviewer is None:
        return None
    if hasattr(reviewer, "attach_sender") and hasattr(reviewer, "_send_http"):
        reviewer.attach_sender(guard.wrap(reviewer._send_http))
        return reviewer
    orig = reviewer.review

    def _guarded_review(**kw):
        mark = guard.reserve_model()
        try:
            out = orig(**kw)
            guard.finish_model(mark, sent_known=True)
            return out
        except Exception:
            guard.finish_model(mark, sent_known=False)
            raise

    reviewer.review = _guarded_review  # type: ignore[method-assign]
    return reviewer


def _ensure_test_budget_run(root: Path, dataset: DatasetSpec, budget: dict,
                            config_fingerprint, *, manifest: dict,
                            status: dict, prepared: list) -> dict:
    """独立 test 账本：独立 lab + EvolutionRun，不复用/不改训练 run 已消费预算。

    状态文件存在但无效、或状态缺失但账本已存在时拒绝，不得清零重建。
    """
    from app.core.skill_evolution import gating as gate, runenv, skill_store
    from app.core.skill_evolution.orchestrator import create_run
    from app.core.skill_evolution.trace_sampling import group_workspace_id
    from app.models.evolution import EvolutionRun

    root = Path(root)
    lab = runenv.ensure_experiment_root(root / TEST_BUDGET_LAB)
    state_path = _test_state_path(root)
    db = skill_store.session_for(lab)
    try:
        existing = db.query(EvolutionRun).first()
        if existing is not None:
            _repair_official_test_state_from_authority(
                root, existing, db, dataset=dataset, manifest=manifest,
                status=status, budget=budget, prepared=prepared)
        if state_path.is_file():
            state = _load_test_state(root)
            if state is None:
                raise BatchError("batch-test 状态文件损坏")
            return _verify_test_state(
                root, state, dataset=dataset, manifest=manifest,
                status=status, budget=budget, prepared=prepared, db=db)
        if existing is not None:
            raise BatchError("test 状态缺失但账本已存在，拒绝清零重建")
        train_ids = [t.task_id for t in dataset.tasks if t.split == "train"]
        val_ids = [t.task_id for t in dataset.tasks if t.split == "val"]
        if not train_ids:
            raise BatchError("test 预算账本缺少 train 任务清单")
        if not val_ids:
            raise BatchError("test 预算账本缺少 val 任务清单")
        ws = group_workspace_id(
            next(t.group_id for t in dataset.tasks if t.split == "train"))
        exp_info = gate.create_experiment(
            db, workspace_id=ws, domain=dataset.domain, dataset=dataset,
            grader_version=dataset.grader_version,
            runner_config={"profile": "faithful", "purpose": "batch-test-ledger",
                           "val_task_ids": val_ids},
            pipeline_key="wiki.default", pipeline_version="3",
            runtime_ref=skill_store.RUNTIME_REF,
            val_task_ids=val_ids, initial_members=[])
        run = create_run(
            db, experiment_id=exp_info.experiment_id, workspace_id=ws,
            domain=dataset.domain, dataset=dataset, init_mode="paper",
            max_iterations=1, budget=dict(budget),
            runner_config={"purpose": "batch-test-ledger",
                           "config_fingerprint": config_fingerprint},
            train_task_ids=train_ids, experience="none", evolve=False)
        plan_hash = manifest.get("plan_hash")
        cfg_fp = status.get("config_fingerprint") or manifest.get("config_fingerprint")
        test_fp = status.get("test_confirm_fingerprint") or (
            (status.get("fingerprints") or {}).get("test_confirm_fingerprint"))
        state = {
            "schema": TEST_STATE_SCHEMA,
            "run_id": run.run_id,
            "experiment_id": exp_info.experiment_id,
            "lab": str(lab),
            "budget": dict(budget),
            "dataset_version": dataset.dataset_version,
            "manifest_plan_hash": plan_hash,
            "config_fingerprint": cfg_fp,
            "test_confirm_fingerprint": test_fp,
            "items": _bind_test_state_items(prepared),
            "completed": [],
            "reports": {},
            "report_hashes": {},
            "in_progress": None,
            "used_model_calls": int(run.used_model_calls or 0),
            "used_tool_calls": int(run.used_tool_calls or 0),
        }
        _save_test_state(root, state, db)
        return state
    finally:
        db.close()


def _pause_batch_test_run(db, run_id: str, token: str) -> None:
    """单条条件 UPDATE 暂停；旧 token 不得清除新 owner 的租约。"""
    from sqlalchemy import text as sql_text
    from app.core.skill_evolution.orchestrator import CLOCK, LeaseConflict
    from app.models.evolution import RUN_PAUSED, RUN_RUNNING

    now = CLOCK.now()
    result = db.execute(
        sql_text(
            "UPDATE evolution_runs SET status = :paused, stop_reason = :reason, "
            "lease_token = NULL, lease_expires_at = NULL, "
            "active_segment_started_at = NULL "
            "WHERE run_id = :run_id AND status = :running "
            "AND lease_token = :token AND lease_owner = :owner "
            "AND lease_expires_at > :now"
        ),
        {
            "paused": RUN_PAUSED,
            "reason": "batch-test",
            "run_id": run_id,
            "running": RUN_RUNNING,
            "token": token,
            "owner": BATCH_TEST_LEASE_OWNER,
            "now": now,
        },
    )
    rowcount = int(result.rowcount or 0)
    if rowcount != 1:
        db.rollback()
        raise LeaseConflict(
            f"batch-test pause CAS 失败 rowcount={rowcount}")
    db.commit()


def _snapshot_test_used(root: Path, state: dict, db=None, *,
                        token: str, expected_sha: str) -> tuple[dict, str]:
    """claim 之后的状态快照：必须带当前 token 做 CAS，丢失租约不得写新 owner。"""
    if db is None:
        raise BatchError("batch-test 状态快照缺少数据库会话")
    _refresh_used_fields(state, db)
    digest = _commit_test_state_fenced(
        root, state, db, token=token, expected_sha=expected_sha)
    return state, digest


def evaluate_batch_test(root: Path, dataset: DatasetSpec, *,
                        executor_runner=None, reviewer=None,
                        consume: bool = False,
                        confirm: dict | None = None) -> dict:
    """两阶段 test：全部预检通过后才创建 test-eval / 构造 actor / 发请求。"""
    root = Path(root)
    manifest = verify_batch_manifest(root, dataset)
    plan = load_batch_plan(root)
    missing = ensure_all_frozen(root, plan["runs"])
    if missing:
        raise BatchError("未全部冻结，拒绝读取 test: " + ",".join(missing))
    status_path = root / "plan" / "batch-status.json"
    status = {}
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
    model_mode = plan.get("model_mode") or status.get("model_mode") or "simulated"

    # —— 阶段 1：遍历全部计划项，零 test-eval、零 actor、零模型请求 ——
    prepared = []
    for item in plan["runs"]:
        state = _read_run_state(Path(item["dir"])) or {}
        lab = Path(state.get("lab") or _replicate_root(
            root, item["protocol"], item["replicate"]))
        frozen = verify_frozen_set(
            Path(item["dir"]),
            run_id=state.get("run_id"),
            experiment_id=state.get("experiment_id"),
            protocol=item["protocol"],
            replicate=item["replicate"],
            lab=lab)
        skills = _skills_from_frozen_item(item, frozen, lab)
        prepared.append({
            "item": item, "state": state, "frozen": frozen, "skills": skills,
        })

    if model_mode == "real":
        if executor_runner is not None or reviewer is not None:
            raise BatchError("real 批次 test 不得传入临时 runner/reviewer")
        budget = _require_real_test_confirm(dataset, manifest, status, confirm)

    # —— 阶段 2：全部通过后才允许 test-eval / actor / 请求 ——
    from app.core.skill_evolution import runenv
    reports = []
    test_state = None
    guard = None
    db = None
    claimed = None
    if model_mode == "real":
        from app.core.skill_evolution import config_freeze, skill_store
        from app.core.skill_evolution.orchestrator import (
            BudgetExceeded, BudgetGuard, claim)
        frozen_cfg = status.get("frozen")
        if not frozen_cfg:
            raise BatchError("real 批次 test 缺少冻结 actor 配置")
        executor_runner = config_freeze.build_actor_runner_frozen(
            "executor", frozen_cfg)
        use_v2_test = "v2" in (dataset.grader_version or "")
        if use_v2_test:
            if not frozen_cfg.get(config_freeze.FROZEN_REVIEWER_KEY):
                raise BatchError("v2 real test 缺少冻结 reviewer")
            reviewer = config_freeze.build_reviewer_frozen(frozen_cfg)
        test_state = _ensure_test_budget_run(
            root, dataset, budget,
            status.get("config_fingerprint") or manifest.get("config_fingerprint"),
            manifest=manifest, status=status, prepared=prepared)
        completed = list(test_state.get("completed") or [])
        saved_reports = dict(test_state.get("reports") or {})
        report_hashes = dict(test_state.get("report_hashes") or {})
        uncertain = _uncertain_test_keys(root, test_state, prepared)
        remaining = [p for p in prepared
                     if _item_key(p["item"]) not in completed]
        if remaining:
            from app.core.skill_evolution.orchestrator import (
                LeaseConflict, LongCallRenewer)
            db = skill_store.session_for(Path(test_state["lab"]))
            renewer = None
            loop_error = None
            expected_sha = None
            try:
                claimed = claim(db, test_state["run_id"],
                                worker_id=BATCH_TEST_LEASE_OWNER)
                token = claimed.lease_token
                live_budget = (json.loads(claimed.config_json or "{}").get("budget")
                               or {})
                if int(live_budget.get("max_model_calls") or 0) != int(
                        budget["max_model_calls"]):
                    raise BatchError("test 预算未挂到独立账本（拒绝使用训练预算）")
                expected_sha = (json.loads(claimed.config_json or "{}")
                                .get("test_state_sha256")
                                or _read_test_state_seal(root))
                if not expected_sha:
                    raise BatchError("test 状态缺少权威 hash，拒绝继续")
                interval = _test_renew_interval(budget)
                renewer = LongCallRenewer(
                    Path(test_state["lab"]), test_state["run_id"],
                    token, interval=interval)
                renewer.start()
                guard = BudgetGuard(db, claimed)
                executor_runner = guard.wrap(executor_runner)
                reviewer = _bind_test_reviewer(reviewer, guard)
                for p in prepared:
                    item, state, frozen, skills = (
                        p["item"], p["state"], p["frozen"], p["skills"])
                    key = _item_key(item)
                    if key in completed:
                        prev = saved_reports.get(key)
                        if prev:
                            reports.append(prev)
                        continue
                    if renewer.lost:
                        raise BatchError("test 租约丢失，拒绝写入 completed")
                    if int(budget["max_model_calls"]) > 0 and \
                            guard.model_available() <= 0:
                        test_state, expected_sha = _snapshot_test_used(
                            root, test_state, db, token=token,
                            expected_sha=expected_sha)
                        raise BatchError("test 模型预算已满（发送前阻止）")
                    test_state["in_progress"] = {"key": key}
                    test_state, expected_sha = _snapshot_test_used(
                        root, test_state, db, token=token,
                        expected_sha=expected_sha)
                    test_lab = runenv.ensure_experiment_root(
                        root / "test-eval" /
                        f"{item['protocol']}{int(item['replicate']):02d}")
                    try:
                        rep = evaluate_frozen_test(
                            test_lab, dataset, skills,
                            executor_runner=executor_runner,
                            reviewer=reviewer)
                    except BudgetExceeded as exc:
                        test_state, expected_sha = _snapshot_test_used(
                            root, test_state, db, token=token,
                            expected_sha=expected_sha)
                        raise BatchError(
                            f"test 模型预算已满（发送前阻止）: {exc}") from exc
                    if renewer.lost:
                        raise BatchError("test 租约丢失，拒绝写入 completed")
                    if guard.budget_exhausted:
                        test_state, expected_sha = _snapshot_test_used(
                            root, test_state, db, token=token,
                            expected_sha=expected_sha)
                        raise BatchError("test 模型预算已满（发送前阻止）")
                    if before_completed_state_commit is not None:
                        before_completed_state_commit({
                            "root": root,
                            "lab": Path(test_state["lab"]),
                            "run_id": test_state["run_id"],
                            "token": token,
                            "claimed": claimed,
                            "renewer": renewer,
                            "key": key,
                            "db": db,
                        })
                    row = {
                        "protocol": item["protocol"],
                        "replicate": item["replicate"],
                        "run_id": state.get("run_id"),
                        "test": rep,
                        "manifest_plan_hash": manifest.get("plan_hash"),
                        "frozen_set_hash": frozen.get("set_hash"),
                        "frozen_members_hash": frozen.get("members_hash"),
                    }
                    if key in uncertain:
                        row["duplicated_after_uncertain_outcome"] = True
                    completed.append(key)
                    saved_reports[key] = row
                    report_hashes[key] = _report_digest(row)
                    test_state["completed"] = completed
                    test_state["reports"] = saved_reports
                    test_state["report_hashes"] = report_hashes
                    test_state["in_progress"] = None
                    test_state, expected_sha = _snapshot_test_used(
                        root, test_state, db, token=token,
                        expected_sha=expected_sha)
                    reports.append(row)
            except BudgetExceeded as exc:
                loop_error = BatchError(
                    f"test 模型预算已满（发送前阻止）: {exc}")
                loop_error.__cause__ = exc
            except Exception as exc:  # noqa: BLE001
                loop_error = exc
            finally:
                if renewer is not None:
                    renewer.stop()
                    if renewer.lost and loop_error is None:
                        loop_error = BatchError(
                            "test 租约丢失，拒绝把未持有租约的结果记为成功")
                if (test_state is not None and claimed is not None
                        and expected_sha and db is not None):
                    try:
                        _snapshot_test_used(
                            root, test_state, db,
                            token=claimed.lease_token,
                            expected_sha=expected_sha)
                    except LeaseConflict as exc:
                        if loop_error is None:
                            loop_error = BatchError(
                                f"test 状态 CAS 失败，拒绝覆盖新 owner: {exc}")
                            loop_error.__cause__ = exc
                    except BatchError as exc:
                        if loop_error is None:
                            loop_error = exc
                pause_error = None
                if claimed is not None and db is not None:
                    try:
                        _pause_batch_test_run(
                            db, claimed.run_id, claimed.lease_token)
                    except LeaseConflict as exc:
                        pause_error = BatchError(
                            f"test 租约冲突，拒绝收口: {exc}")
                        pause_error.__cause__ = exc
                if db is not None:
                    db.close()
            if loop_error is not None:
                raise loop_error
            if pause_error is not None:
                raise pause_error
        else:
            for p in prepared:
                key = _item_key(p["item"])
                prev = saved_reports.get(key)
                if prev:
                    reports.append(prev)
    else:
        for p in prepared:
            item, state, frozen, skills = (
                p["item"], p["state"], p["frozen"], p["skills"])
            test_lab = runenv.ensure_experiment_root(
                root / "test-eval" /
                f"{item['protocol']}{int(item['replicate']):02d}")
            rep = evaluate_frozen_test(
                test_lab, dataset, skills, executor_runner=executor_runner,
                reviewer=reviewer)
            reports.append({
                "protocol": item["protocol"], "replicate": item["replicate"],
                "run_id": state.get("run_id"),
                "test": rep,
                "manifest_plan_hash": manifest.get("plan_hash"),
                "frozen_set_hash": frozen.get("set_hash"),
                "frozen_members_hash": frozen.get("members_hash"),
            })
    if consume:
        mark_test_consumed(root, dataset.dataset_version)
    return {"dataset_version": dataset.dataset_version, "reports": reports,
            "manifest_plan_hash": manifest.get("plan_hash"),
            "note": "保留全部 replicate，不选最好一次；小样本不宣布提升。"}
