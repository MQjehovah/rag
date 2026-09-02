"""Phase 4：KnowledgeCompileRun 编译流水线框架（框架 + FakePipeline）。

包结构：
- state_machine.py：Run/Stage 状态转换集中管理（禁止各函数任意写字符串）。
- registry.py：StageDef/PipelineDef 注册表（register/get）。
- fake.py：可测试的 FakePipeline/Stage（只记录执行，不产生真实副作用）。
- executor.py：串行执行 run（stage 行 + artifact 行 + 状态/attempt/缓存）。
- worker.py：claim/heartbeat/requeue_stale/recover_startup + 进程内 Worker 线程。

Phase 4 只建框架与 FakePipeline，不迁移现有默认 Wiki 编译
（wiki_page_builder/wiki_refresh_scheduler 保持原样）。
"""
