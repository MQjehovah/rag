"""实验运行环境：路径边界、实验专用数据库、编译栈注册。

隔离边界（阶段 1）：
- 实验根目录必须显式指定；入口在根目录创建品牌文件标识（拒绝把任意目录当根）。
- 每个 execution 的 SQLite 由本模块在 runs/<execution_id>/ 下全新创建；DB 文件已
  存在 → 拒绝（绝不打开既有数据库文件，包括业务库）。
- 数据库文件解析路径与 settings.database_url（若为 sqlite 文件）比对，相等 → 拒绝；
  本模块绝不读取/回退 settings.database_url。
- 编译栈（builtin skills + wiki.default v1/v2/v3）在此进程内注册；实验不启动
  worker pump、不调用 scheduler/API。
- graph 阶段由调用方注入 GraphRecorder（写生产资源会经 _default_graph_runner 的
  settings 数据库路径，必须替换）。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core.skill_evolution.errors import SkillEvolutionError

BRAND_FILENAME = "wikiskill-evolution-root.json"
_BRAND = {"brand": "wikiskill-evolution-root", "version": 1}
RUNS_DIR = "runs"
DB_FILENAME = "experiment.db"
PUBLIC_ACL = '{"groups": ["__public__"]}'
PUBLIC_SCOPE = "company"


class ExperimentPathError(SkillEvolutionError):
    """实验根/数据库路径不合法。"""


def ensure_experiment_root(root: Path) -> Path:
    """创建/校验实验根并写入品牌标识（标识由实验入口创建）。"""
    root = Path(root).resolve()
    brand = root / BRAND_FILENAME
    if root.exists():
        if not root.is_dir():
            raise ExperimentPathError(f"实验根不是目录: {root}")
        if brand.is_file():
            try:
                data = json.loads(brand.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ExperimentPathError(f"实验根品牌文件损坏: {root}") from exc
            if data.get("brand") != _BRAND["brand"]:
                raise ExperimentPathError(f"实验根品牌不匹配: {root}")
            return root
        # 目录存在但无品牌：拒绝（避免把任意既有目录当实验根）。
        raise ExperimentPathError(
            f"目录已存在且不是 wikiskill 实验根（缺品牌文件）: {root}。"
            "请指定新的/显式初始化的实验根。")
    root.mkdir(parents=True, exist_ok=False)
    brand.write_text(json.dumps(_BRAND, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / RUNS_DIR).mkdir(exist_ok=True)
    return root


def _sqlite_file_from_database_url(url: str) -> Path | None:
    if not url or not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    if raw in ("", ":memory:"):
        return None
    p = Path(raw.split("?", 1)[0])
    try:
        return p.resolve()
    except OSError:
        return None


def reject_unsafe_db_path(db_path: Path, root: Path) -> None:
    """DB 必须位于实验根内、为新建文件，且不等于任何配置中的 sqlite 业务库。"""
    db_path = Path(db_path).resolve()
    root = root.resolve()
    if not db_path.is_relative_to(root):
        raise ExperimentPathError(f"实验数据库必须位于实验根内: {db_path}")
    if db_path.exists():
        raise ExperimentPathError(f"实验数据库文件已存在，拒绝打开既有库: {db_path}")
    configured = _sqlite_file_from_database_url(settings.database_url)
    if configured is not None and db_path == configured:
        raise ExperimentPathError("实验数据库指向配置中的业务数据库，已拒绝")


def execution_db_path(root: Path, execution_id: str) -> Path:
    return root / RUNS_DIR / execution_id / DB_FILENAME


def new_execution_dir(root: Path, execution_id: str | None = None) -> tuple[str, Path]:
    """创建 runs/<execution_id>/ 目录并返回 (execution_id, dir)。

    若传入预分配 execution_id：目录已存在则复用（不得换成另一个 id）。
    """
    root = Path(root).resolve()
    if execution_id:
        run_dir = root / RUNS_DIR / execution_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return execution_id, run_dir
    execution_id = uuid.uuid4().hex
    run_dir = root / RUNS_DIR / execution_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return execution_id, run_dir


def make_experiment_engine(db_path: Path):
    """创建实验专用 SQLite engine（外键启用；与既有测试库同构）。"""
    url = f"sqlite:///{(Path(db_path).as_posix())}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def make_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()


def register_compile_stack() -> dict:
    """进程内注册 builtin skills + wiki.default v1/v2/v3（幂等），返回注册摘要。

    与生产 bootstrap 的差异：不调用 set_active 依赖 settings —— 阶段 1 显式固定
    pipeline_version="3"（见 executor.create_run 的显式版本语义）。
    """
    from app.core.wiki_pipeline import registry as pregs
    from app.core.wiki_pipeline.pipelines.wiki_default import (
        PIPELINE_KEY,
        register_default_pipeline,
    )
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default import (
        register_default_pipeline_v2,
    )
    from app.core.wiki_pipeline.pipelines.wiki_skilled_default_v3 import (
        register_default_pipeline_v3,
    )
    from app.core.wiki_skills import registry as sregs
    from app.core.wiki_skills import service as skill_service

    skill_service.register_builtin_skills()
    register_default_pipeline()      # v1
    register_default_pipeline_v2()   # v2
    register_default_pipeline_v3()   # v3
    summary = {
        "pipeline_key": PIPELINE_KEY,
        "pipeline_versions": sorted(pregs.REGISTRY.get(PIPELINE_KEY, {}).keys()),
        "skills": {
            key: list(sregs.registered_versions(key))
            for key in sorted({s["key"] for s in sregs.list_skills()})
        },
        "python_version": sys.version.split()[0],
    }
    return summary


def runtime_summary() -> dict:
    """运行时可复现性摘要（注册快照）。"""
    from app.core.wiki_pipeline import registry as pregs
    from app.core.wiki_pipeline.pipelines.wiki_default import PIPELINE_KEY
    from app.core.wiki_skills import registry as sregs

    versions = list(pregs.REGISTRY.get(PIPELINE_KEY, {}).keys())
    skills = {}
    for key in sorted({s["key"] for s in sregs.list_skills()}):
        skills[key] = list(sregs.registered_versions(key))
    return {
        "pipeline_key": PIPELINE_KEY,
        "pipeline_versions": versions,
        "skills": skills,
        "python_version": sys.version.split()[0],
    }
