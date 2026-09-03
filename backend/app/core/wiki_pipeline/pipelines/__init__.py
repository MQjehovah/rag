"""Phase 5：真实产品 pipeline 实现包（wiki.default 等）。

- dto.py：跨 stage 传递的结构化 DTO（frozen dataclass，全部可 json.dumps）。
- wiki_default.py：默认 Wiki 编译流水线（wiki.default v1）+ register_default_pipeline()。
- wiki_skilled_default.py：Phase 6 wiki.default v2（skill-aware）+ register_skilled_default_pipeline()。
"""
