"""Phase H-2 应用 smoke test（只读，绝不调用真实 LLM/Embedding/Reranker）。

验证四件事，任一失败非零退出（供 runbook 作为「smoke 失败禁止重启服务」门禁）：
1. 应用可 import（import app.main 成功，且不加载 Card/KO/旧治理模块）；
2. V4 路由存在：Chat / Search / Wiki / Community / Debt；
3. Card / KO / 旧治理路由不存在；
4. V4 ORM 只读查询可执行（只读 URI，不写库、不调用模型）。

用法（在 backend 目录下）：
    python scripts/smoke_h2.py --db data/notes.db

--db 缺省时只做 import + 路由检查（不触碰任何数据库文件）。
"""
from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

# 必须存在的 V4 路由前缀（子串匹配 app.routes 中的 path）。
REQUIRED_ROUTE_PREFIXES = {
    "chat": "/api/chat",            # 旧入口（保留）
    "rag_chat": "/api/rag-chat",    # V4 问答（降级）
    "search": "/api/search",        # 知识搜索
    "wiki": "/api/wiki",            # Wiki 主题
    "community": "/api/v4/graph",   # V4 图谱 / Community
    "debt": "/api/v4/debts",        # V4 知识债务
}

# 必须不存在的旧路由前缀。
FORBIDDEN_ROUTE_PREFIXES = {
    "/api/cards",           # Card
    "/api/conflicts",       # 冲突中心
    "/api/governance",      # 治理工作台
    "/api/p5",              # 旧 P5 图谱
    "/api/knowledge/debts",  # 旧债务入口
}

# 导入 app.main 后不得出现在 sys.modules 的旧模块。
FORBIDDEN_MODULES = (
    "app.api.cards", "app.api.conflicts", "app.api.governance", "app.api.p5_graph",
    "app.api.debts", "app.api.legacy",
    "app.core.knowledge_compiler.conflict_center",
    "app.core.knowledge_compiler_v3.pipeline",
    "app.core.knowledge_compiler_v3.persistence",
    "app.core.knowledge_compiler_v3.card_builder",
    "app.core.knowledge_compiler_v3.card_graph",
    "app.core.knowledge_compiler_v3.community",
    "app.core.knowledge_compiler_v3.wiki_builder",
    "app.core.knowledge_compiler_v3.wiki_links",
    "app.core.retrieval.qa", "app.core.retrieval.pipeline", "app.core.retrieval.dense",
    "app.core.graph", "app.core.evidence_validator", "app.core.legacy_ko_inventory",
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"[PASS] {msg}")


def check_import_and_routes() -> None:
    """导入 app.main，校验 V4 路由存在、旧路由不存在。"""
    app = importlib.import_module("app.main").app
    loaded_legacy = [m for m in FORBIDDEN_MODULES if m in sys.modules]
    if loaded_legacy:
        _fail(f"app.main 加载了旧模块：{loaded_legacy}")
    _ok("应用可 import（app.main），且未加载 Card/KO/旧治理模块")

    paths = []
    for r in app.routes:
        # FastAPI 0.141+ 的 include_router 是惰性的（_IncludedRouter），
        # 需从 original_router.routes 展开子路由路径。
        orig = getattr(r, "original_router", None)
        if orig is not None:
            for rr in orig.routes:
                p = getattr(rr, "path", "")
                if p:
                    paths.append(p)
        else:
            p = getattr(r, "path", "")
            if p:
                paths.append(p)

    for label, prefix in REQUIRED_ROUTE_PREFIXES.items():
        if not any(p.startswith(prefix) for p in paths):
            _fail(f"缺少 V4 路由 {label}（前缀 {prefix}）")
        _ok(f"V4 路由存在：{label}（{prefix}）")

    for prefix in FORBIDDEN_ROUTE_PREFIXES:
        if any(p.startswith(prefix) for p in paths):
            _fail(f"检测到旧路由：{prefix}")
    _ok("Card / KO / 旧治理路由不存在")


def check_orm_readonly(db_path: str) -> None:
    """用只读 URI + V4 ORM 做只读查询，证明 V4 只读路径可执行。"""
    p = Path(db_path).expanduser().resolve()
    if not p.exists() or p.is_dir():
        _fail(f"数据库文件不存在或为目录：{p}")

    # 只读 URI：绝不写库。
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    uri = f"sqlite:///file:{p.as_posix()}?mode=ro&uri=true"
    engine = create_engine(uri, connect_args={"check_same_thread": False})

    try:
        # V4 ORM 只读查询：knowledge_debts 行数。
        from app.models.database import KnowledgeDebt, WikiPage, SourceConnection
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            debt_count = session.query(KnowledgeDebt).count()
            _ok(f"V4 ORM 只读查询可执行（knowledge_debts 行数 = {debt_count}）")
        finally:
            session.close()

        # V4 关键保留表存在性（原始只读 SQL）。
        with engine.connect() as conn:
            for table in ("wiki_pages", "knowledge_debts", "knowledge_debt_users",
                          "runtime_feature_flags", "source_connections"):
                rows = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).fetchone()
                if rows is None:
                    _fail(f"V4 保留表 {table} 不可读")
            _ok("V4 关键保留表可只读访问")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase H-2 应用 smoke test（只读）")
    parser.add_argument("--db", help="数据库路径（可选，缺省时不做库查询）")
    args = parser.parse_args()

    check_import_and_routes()
    if args.db:
        check_orm_readonly(args.db)
    print("SMOKE_OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
