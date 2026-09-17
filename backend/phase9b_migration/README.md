# Phase 9B 迁移工具箱（真实库副本迁移 / Workspace 回填 / 恢复演练）

只依赖 **标准库 + 仓库 venv 已有依赖**（sqlalchemy / alembic / pydantic）；
**绝不 import `app.main` / worker / startup**，运行期也不碰 `app.main`。

**红线**：默认拒绝写 `backend/data/notes.db`（guard 按路径识别，win32 大小写不敏感）；
写库必须先给 `--allowed-dir`，目标越界一律非 0 退出。

## 子命令一览

| 命令 | 用途 | 写库 | 需 `--allowed-dir` | 允许真实库作 `--db` |
| --- | --- | --- | --- | --- |
| `inventory --db <db> [--out report.md]` | 只读盘点（表/行数/ACL 分布/回填候选/blocked/同 ACL/同名 Wiki/orphan，id 一律 sha256[:12] 脱敏） | 否 | 否 | 是（只读） |
| `snapshot --db <src> --allowed-dir <dir> [--ts T]` | 建会话（source-snapshot/working/restored/reports/logs），sqlite backup API 出 baseline，再拷 working，附 baseline inventory 报告 | 是（落新文件） | 是 | 是（只读源，工具存在意义） |
| `preflight --db <db>` | 升级前只读体检：quick_check / foreign_key_check / revision | 否 | 否 | 是（只读） |
| `upgrade --db <db> --allowed-dir <dir> [--target head]` | alembic upgrade（子进程，cwd=backend，env 显式 `DATABASE_URL`，`-x database_url=` 双保险） | 是 | 是 | 否 |
| `downgrade --db <db> --rev <rev> --allowed-dir <dir>` | alembic downgrade 到指定 rev | 是 | 是 | 否 |
| `backfill-dryrun --db <db>` | 回填计划 + 确定性 `plan_hash`（只读 SELECT） | 否 | 否（仅限副本） | 否 |
| `backfill-apply --db <db> --plan-hash <hash> --allowed-dir <dir>` | 单事务回填；hash 不一致拒绝；二次 apply `created=0`（幂等） | 是 | 是 | 否 |
| `postflight --db <db> --allowed-dir <dir> [--baseline-json c.json]` | 回填/迁移后置检查 + 合成反例 rollback 事务 | 是（合成事务后回滚） | 是 | 否 |
| `downgrade-drill --db <migrated> --allowed-dir <dir> [--rev 653bbcf9847b]` | 副本降级到当前 head 的直接父版本（默认 P52）→ 评估 → 升回 head | 是 | 是 | 否 |
| `restore-verify --db <baseline> --allowed-dir <dir> [--restored out.db]` | baseline 恢复副本并校验 sha 完全一致 + quick/版本/计数 | 是 | 是 | 否 |

示例（默认 `--real-db` 为 `backend/data/notes.db`，测试/演练请用 `--real-db` 指向假 real 以便本地复核防误写）：

```
# 1) 只读盘点真实库副本
python -m phase9b_migration.main inventory --db some-copy.db --out report.md

# 2) 快照 + working
python -m phase9b_migration.main snapshot --db backend/data/notes.db --allowed-dir C:\tmp\drill

# 3) working 副本 preflight/upgrade（--db 指向 working 副本）
python -m phase9b_migration.main upgrade --db C:\tmp\drill\...\work.db --allowed-dir C:\tmp\drill

# 4) 回填
python -m phase9b_migration.main backfill-dryrun --db work.db
python -m phase9b_migration.main backfill-apply --db work.db --plan-hash <hash> --allowed-dir C:\tmp\drill

# 5) 后置检查 / 恢复演练
python -m phase9b_migration.main postflight --db work.db --allowed-dir C:\tmp\drill
python -m phase9b_migration.main downgrade-drill --db work.db --allowed-dir C:\tmp\drill
python -m phase9b_migration.main restore-verify --db baseline.db --allowed-dir C:\tmp\drill
```

## 安全约束（代码层）

- `guard.write_guard(target, allowed_dir)`：target 是真实库 **或** 不在 allowed_dir
  内 → `WriteGuardError`；读永远走 `guard.readonly_conn`（`file:...?mode=ro`）。
- upgrade/downgrade 由 `backend/` 下子进程执行：
  `python -m alembic -x database_url=<url> ...`，env 同时显式设 `DATABASE_URL`。
- 回填复用 production 语义（单一事实来源）：
  - `app/core/access_control.py`：`scope_from_notebook`（186）、`normalize_scope_id`
    （330）、`workspace_key_for_notebook`（345）、`acl_json_for_scope`（364）；
  - `app/core/wiki_workspace/routing.py:89-142` 的 `ensure_notebook_workspace`
    决策优先级被 backfill/inventory 逐条镜像。
- 所有新增内容 UTF-8 无 BOM；报告不含正文/凭证/完整 id。

## 演练顺序建议

1. **inventory**（真实库或最新备份副本）——记录 head 前状态、回填候选/blocked 基线；
2. **snapshot** —— baseline + working；对 baseline 跑 **preflight**；
3. **upgrade**（working 副本到 head）→ 再次 **preflight**；
4. **backfill-dryrun** → 复核候选/blocked → **backfill-apply**（记住 plan_hash）；
   再 dryrun 应 `created=0`；重复 apply 幂等返回 0；
5. **postflight**（快速体检 / active binding 唯一 / blocked 未放行 / 默认 workspace
   不合并 / wiki.workspace_id 一致 / published wiki NULL workspace 如实计数 /
   数量不意外减少 / Section 可读 / 合成反例 rollback）；
6. **downgrade-drill**（升回 head 幂等、数据不丢）与 **restore-verify**（sha 一致）
   作为恢复演练；全部通过后才可对生产库执行正式迁移。

> 注意：`backfill` / `postflight` / `recovery` 全部只应在**副本**上执行；`snapshot`
> 是唯一允许把真实库当只读源的命令。真实库正式升级仍应按仓库规程先完整备份。
