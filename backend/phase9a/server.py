# -*- coding: utf-8 -*-
"""Phase 9A server：启动隔离真实后端（CONTRACT §1/§2 服务端模型替身）。

环境（由 orchestrator 注入，本脚本先读一次 env 做守卫校验）：
    DATABASE_URL          sqlite 绝对 URL（必须位于 <session>\\db 下）
    PHASE9A_SESSION_DIR   <repo>\\.phase9a\\<session>   （DB 路径守卫基准）
    PHASE9A_BE_PORT       端口（默认 8810，绑定 127.0.0.1）
    PHASE9A_RECORD_FILE   调用记录 JSONL（必填；缺失即退出）
    PHASE9A_FAULT_FLAG    故障注入开关文件（可空）
    LDAP_GROUP_MAP_WIKI_EDITOR=editors 等（由 orchestrator 注入）
    WIKI_TOPIC_ENABLED=true、AUTO_DAILY_SCAN_ENABLED=false、AUTO_ORGANIZE_ENABLED=false

守卫：
- DB resolve 路径必须严格位于 <session>\\db 下，否则非零退出；
- 没有替身记录文件 env 则报错退出；
- 不连真实模型：断言 LLM_API_URL / EMBEDDING_API_URL / RERANKER_API_URL 为空。

然后：
- 确定性 Embedding（同 conftest fake 语义，类级替换）；
- executor.configure_external_runners(llm_runner=rec, graph_runner=graph_rec)；
  rec 按 CONTRACT §2（记录 JSONL + fault flag 注入 + block marker 轮询）；
  graph_rec 记录后委托真实 wiki_default._default_graph_runner 建图；
- 以 uvicorn.run(app.main.app) 启动真实 lifespan（bootstrap + worker 泵）。

Windows：import app.* 前无需改 env（env 已在进程外设置）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_REPO_ROOT = _BACKEND.parent
_DEFAULT_PORT = 8810


def _fail(message: str) -> int:
    print(f"[phase9a] server 启动拒绝: {message}", file=sys.stderr)
    return 2


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    record_file = os.environ.get("PHASE9A_RECORD_FILE", "").strip()
    fault_flag = os.environ.get("PHASE9A_FAULT_FLAG", "").strip() or None
    session_dir = os.environ.get("PHASE9A_SESSION_DIR", "").strip()
    try:
        port = int(os.environ.get("PHASE9A_BE_PORT", str(_DEFAULT_PORT)).strip())
    except ValueError:
        return _fail(f"PHASE9A_BE_PORT 非法: {os.environ.get('PHASE9A_BE_PORT')}")
    host = "127.0.0.1"

    # ---- 守卫：记录文件必须提供 ----
    if not record_file:
        return _fail("缺少 PHASE9A_RECORD_FILE（替身调用记录必须落盘）")
    if not database_url.startswith("sqlite:///"):
        return _fail("DATABASE_URL 必须是 sqlite 绝对 URL")
    db_rel = database_url[len("sqlite:///"):]
    db_path = Path(db_rel).resolve(strict=False)
    if not session_dir:
        return _fail("缺少 PHASE9A_SESSION_DIR（用于 DB 路径守卫）")
    allowed_root = (Path(session_dir).resolve(strict=False) / "db").resolve(strict=False)
    try:
        ok = db_path.is_relative_to(allowed_root)
    except AttributeError:  # Python < 3.9
        ok = str(db_path).startswith(str(allowed_root))
    if not ok:
        return _fail(f"DB 路径越界: {db_path} 不在 {allowed_root} 下")

    # ---- 守卫：不连真实模型（空地址兜底必须不命中真实客户端）----
    for var in ("LLM_API_URL", "EMBEDDING_API_URL", "RERANKER_API_URL"):
        value = os.environ.get(var, "").strip()
        if value:
            return _fail(f"{var} 必须为空（替身进程不连真实模型）: {var}={value!r}")

    from app.config import settings
    from app.core.wiki_pipeline import executor
    from app.core.wiki_pipeline.pipelines import wiki_default as wd
    from phase9a import fixtures

    # ---- 确定性 Embedding（与 conftest fake 语义一致）----
    fixtures.patch_embedding_fake(settings)

    # ---- recorder + 外部 runner（CONTRACT §2）----
    recorder = fixtures.Recorder(record_file, fault_flag)
    executor.configure_external_runners(
        llm_runner=recorder.llm_runner(),
        graph_runner=recorder.graph_runner(wd._default_graph_runner),
    )
    print(f"[phase9a] recorder -> {Path(record_file).resolve()}")

    # ---- 真实 lifespan 启动（worker 泵由 app.main startup 经 run_startup_recovery
    #      后启动；本进程不再提前 start_worker）----
    import uvicorn

    from app.main import app

    print(f"[phase9a] uvicorn {host}:{port} (DB={db_path})")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
