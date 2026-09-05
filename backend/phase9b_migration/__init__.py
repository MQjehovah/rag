"""Phase 9B migration toolbox —— 真实库副本迁移 / Workspace 回填 / 恢复演练工具包。

本包与 app.* 解耦：默认只依赖标准库 + SQLAlchemy/pydantic（仓库 venv 已有），
绝不 import app.main / worker / startup。所有写库操作必须先过 phase9b_migration.guard。
"""
