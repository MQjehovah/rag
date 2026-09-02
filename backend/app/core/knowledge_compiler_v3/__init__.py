"""Knowledge Card V3 编译流水线（P2，V3 计划 7.3）。

模块：
- worthiness.py   → 第 1 步 Knowledge Worthiness Gate
- claims.py       → 第 2 步 Claim Extraction（Evidence-aware）
- cluster.py      → 第 3 步 文档内聚类
- card_builder.py → 第 4 步跨文档匹配 + 第 5 步生成 Card
- matcher.py      → ExistingCardMatcher
- diff.py         → CardDiffService
- quality.py      → CardQualityGate
- pipeline.py     → 编排入口
"""
