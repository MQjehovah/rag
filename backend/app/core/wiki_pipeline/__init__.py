"""Phase 4/5：KnowledgeCompileRun 编译流水线框架 + 真实产品 pipeline。

包结构：
- state_machine.py：Run/Stage 状态转换集中管理（禁止各函数任意写字符串）。
- registry.py：StageDef/PipelineDef 注册表（register/get）。
- fake.py：可测试的 FakePipeline/Stage（只记录执行，不产生真实副作用）。
- executor.py：串行执行 run（stage 行 + artifact 行 + 状态/attempt/缓存）。
- worker.py：claim/heartbeat/requeue_stale/recover_startup + 进程内 Worker 线程。
- pipelines/：Phase 5 真实产品 pipeline（wiki.default 等）与 stage DTO。

导出：
- register_default_pipeline / unregister_default_pipeline：注册/清理 wiki.default
  （main.py startup 接线与测试隔离用）。
"""
from app.core.wiki_pipeline.pipelines.wiki_default import (
    register_default_pipeline,
    unregister_default_pipeline,
)

__all__ = [
    "register_default_pipeline",
    "unregister_default_pipeline",
]
