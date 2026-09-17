# WikiSkill 隔离 PostgreSQL/pgvector 接入验收

> 判定：**隔离工程验收通过**。不是生产验收，不是真实模型效果验收。
> 日期：2026-09-09。运行标签：`wikiskill.acceptance.run=20260908`。
> 凭据：测试口令仅存在于本机进程环境，未写入仓库、文档或报告。

## 环境与隔离

| 项 | 值 |
| --- | --- |
| Docker Desktop | 4.90.0（本机已具备） |
| Docker Engine | 29.7.2（Client/Server） |
| WSL | 2.7.13 |
| 镜像 | `pgvector/pgvector:pg16`（`sha256:ccc6e83d6e35…`，验收后保留） |
| PostgreSQL | 16.15 (Debian 16.15-1.pgdg12+2) |
| pgvector | 0.8.6 |
| 容器 | `wikiskill-pg-acceptance-20260908` |
| 标签 | `wikiskill.acceptance=true`，`wikiskill.acceptance.run=20260908` |
| bind | 仅 `127.0.0.1:55432→5432` |
| volume | `wikiskill_pg_acceptance_20260908`（同标签） |
| 数据库 / 用户 | `wikiskill_acceptance` / `wikiskill_test` |
| URL 形态 | `postgresql+psycopg2://wikiskill_test:***@127.0.0.1:55432/wikiskill_acceptance` |

隔离约束（已执行）：

- 未读取 `backend/.env`，未使用项目 `docker-compose.yml`。
- 未连接默认 5432，未启动生产后端 / worker / 真实模型，未访问模型供应商。
- Alembic 仅在显式 `-x database_url=` 时运行；`env.py` 此时不 import `app.config`。
- 验收工具 `backend/tools/pg_wikiskill_acceptance.py`：必须显式 URL；host 仅 127.0.0.1/localhost；端口必须 55432；库名须含 acceptance/test；拒绝 SQLite / 5432 / 远程 host；默认 fail-closed；输出脱敏。

启动前：发现同名旧容器（2026-09-08 创建、标签匹配本任务、volume 当时无标签、口令未知）。判定归属本任务后重建并给 volume 打标签，未删除无法证明归属的资源。

## 迁移

当前单 head：`4f83c9e2a1d7`（P53）。未新增 P54。未改 P45–P53。生产库未执行。

| 路径 | 结果 |
| --- | --- |
| 空库 `upgrade head` | 成功；`alembic_version=4f83c9e2a1d7`；重复 upgrade 幂等 |
| 空库 → P44 → head | 成功（P45–P53 全链） |
| 空库 → P51（旧单技能绑定）→ P52 → P53/head | 成功：`rev` 默认正确；`members_json=NULL` 读为旧单技能；`set_hash` 按兼容逻辑补齐；不丢绑定；不产生虚假集合成员；审计保留 |

P45–P53 表/列/索引/约束齐全：`EvolutionSkillBinding.rev/set_hash/members_json`，P53 事件集合列（`from/to_members_json`、`from/to_set_hash`、`from/to_rev`），`ux_evolution_binding_scope`、`ux_evolution_business_event_idem`、`ck_evolution_binding_pair`。

## PG 专项（stage8u）

文件：`backend/tests/test_skill_evolution_stage8u_postgresql.py`。

仅当显式设置 `WIKISKILL_PG_ACCEPTANCE_URL` 时收集执行；普通 pytest 无 URL 则 skip，不得把未执行写成通过。本轮已显式启用，**22 passed / 0 skip**（32.84s）。

| 节 | 结果 |
| --- | --- |
| A 基础 | PG16 可连；pgvector 可建可查；venv 内 psycopg2/SQLAlchemy；提交/回滚/隔离连接；全部在隔离库 |
| D 集合绑定 | 旧单技能兼容读；多技能规范顺序读写；`set_hash` 与 canonical members 一致；promote/rollback 全有或全无；审计同事务；审计失败整体回滚；编译注入完整集合；在途 run 用冻结集合，新 run 读当前/回退后的集合 |
| E CAS/并发/ABA | 双独立 Engine/Session；正确令牌成功；陈旧 rev/hash → `binding_conflict`；同 rev 竞争仅一赢；首绑竞争仅一赢；v1→v2→v1 旧 rev 不能再写；失败事务不覆盖成功事务；barrier 协调，不用 sleep |
| F 幂等 | 同 key 同体只执行一次；不重复增 rev/审计；不同体 → `idem_content_conflict`；回退后新 key 可再晋升；丢响应重试返回原结果；指纹覆盖完整成员 + expected_rev + expected_set_hash；并发同 key 一写一重放、不双写 |
| G P53 门禁 | 精确集合哈希/接受事件/评估/数据集与 grader 指纹/冻结配置/批准证据路径仍生效；v2 `labels_approved=true`、`real_calibration=false`、`calibration=engineering_only`、`allow_business_promotion=false`；PG 上正式晋升仍拒绝；人工标签不能绕过真实校准；v1 mechanism 不得冒充真实效果 |
| H 重启 | 关 Session/Engine 后新 Engine 重连：记录完整、预算与冻结配置保持、rev/set_hash 不重置、幂等仍有效、旧 run 不读新绑定 |
| I 备份恢复 | 容器内 `pg_dump -Fc` / `pg_restore`；原库与恢复库 alembic_version、关键表数量、绑定集合、rev/set_hash、审计、冻结、幂等一致 |

## 备份恢复摘要

报告目录：`backend/reports/pg-acceptance-20260909-091915/`（目录内无口令）。

- dump：`wikiskill_acceptance.dump`（213696 bytes，sha256 `f207aef65b4d69bd…`）
- 恢复库对比 digest：`922b52b6eac58bd2…`
- 计数一致：bindings 51、events 102、versions 129、runs 12、compile artifacts 20、`alembic_version` 1
- 最新绑定：`rev=1`，`set_hash=17f3696174376cb3…` 两侧相同
- 恢复库在保存脱敏证据后删除

## 实际修复

WikiSkill P45–P53 无 schema 缺陷，**未写 P54**。

1. **P25/P26/P28 `batch_alter_table(recreate="always")`**
   PG 会先 DROP PK，被 FK 挡住（如 `query_logs_pkey` ← `knowledge_debt_queries`）。SQLite 整表重建无此检查。
   修复：`migration_compat.drop_columns_compat`（SQLite 仍 recreate；PG 用 `ALTER DROP COLUMN`）。属空库 upgrade 中段失败，P54 无法回溯。

2. **P31/P34 整数当布尔**
   SQLite Boolean=INTEGER 接受 `1/0`；PG 严格 boolean。改为 `TRUE/FALSE`。

3. **P32.5 `HAVING c > 1`**
   PG 16 不允许 HAVING 别名；改为 `HAVING COUNT(*) > 1`。
   `get_columns()` 在 PG 上 `primary_key` 为 None；改用 `get_pk_constraint`。

4. **并发同幂等键在 PG 上无法重放**
   PG 上 `IntegrityError` 使事务 aborted，原实现 rollback 后直接抛 `binding_conflict`。SQLite 可在同一事务继续，旧测试看不到。
   修复：`business_ops.promote/rollback` 在 IntegrityError / CAS `binding_conflict` 后 rollback，再按幂等键重放原结果。不靠 sleep、不串行化、不放宽断言。

5. **Alembic `env.py`**
   有 `-x database_url=` 时不加载 `.env`。

6. **文案**
   `v2-acceptance-review.json` 仅改顶层 `note`：A1–A5 已由 human-project-owner 于 2026-09-08 批准；`used_for_development=false`；仅人工参考标签；不代表真实校准；不允许业务晋升。samples 未改。

## 历史迁移兼容修正例外

本轮为使空库能升级到 P45 之前，对 **P45 之前** 的历史迁移做了 SQLite/PostgreSQL 方言等价修正。P54 无法修复这些失败：Alembic 在 PostgreSQL 上整链一层事务，P25–P34 中段失败会回滚，永远到不了后置 head。

修改仅为方言兼容，**没有改变** `revision` / `down_revision`，也没有改变业务数据语义。P45–P53 未改。

| 文件 | git HEAD SHA-256 | 工作区 SHA-256 |
| --- | --- | --- |
| `backend/alembic/versions/f1a2b3c4d5e6_p25_remove_legacy_graph.py` | `9396bec79bf16ef1b1ef6bc51828704d98ee698244ba7f91b81354e340231748` | `5d37b9e9ee1502437cc4768418513d24d2182aa299b6fd88f366c22fc05e8f80` |
| `backend/alembic/versions/a2b3c4d5e6f7_p26_remove_legacy_ui_fields.py` | `ddbcaa9a0c051546f6d5021b7ad2afd8374e28c0b2c84ac0913adb7615451079` | `14ef640f22940e10e5b83783e2317f8fd842e9f5d60e5df3bb9821a800af5555` |
| `backend/alembic/versions/f5a6b7c8d9e0_p28_remove_owner_and_legacy_mapping.py` | `f77b72acba85bed89ffae3967422b1674d54df8e6ae60e792472700637744625` | `13a1b4252813875b51a4ed49cac4c3fec4cc5a1d823b4a66e7b2393f5f02a4c1` |
| `backend/alembic/versions/b8e9f0a1b2c3_p31_wiki_page_driven_fields.py` | `ef1295e709c4c3a0f07a57588eff3c1be6b06e42bdd01cf4d0aea3083c6e015e` | `8673b4d9ef6ed21df143ef94e132e7698ce82f929450108e2d27578793227b26` |
| `backend/alembic/versions/a8b9c0d1e2f3_p32_5_reconcile_debt_schema.py` | `58f9fd4bf7a2cc5ff4ec1c000c70b5c92f0a1f096754a18a7379714ed9665730` | `f4e3d082cc7bb49e2026c3cb461ca58473c6d927ebf9070a4e31df9d66b9290e` |
| `backend/alembic/versions/e5f6a7b8c9d0_p34_version_aware_wiki.py` | `831c0e9f0fb95bbdac9a74a1c9e0a4b0df48d88d8fd2aef8ad8981fe081bea85` | `a7309eb3191465b886fd70605d66f7a64260fffdea38709abe216a558f50c65f` |

方言差异摘要：

- P25/P26/P28：`batch_alter_table(recreate="always")` 在 PG 上会先 DROP PK，被 FK 挡住；改为 `drop_columns_compat`（SQLite 仍 recreate，PG 用 `ALTER DROP COLUMN`）。
- P31/P34：布尔字面量 `1/0` 改为 `TRUE/FALSE`（SQLite 同样接受）。
- P32.5：`HAVING` 别名改为 `HAVING COUNT(*) > 1`；PK 断言改用 `get_pk_constraint`。

**以后不得继续任意修改历史迁移。** 新增问题优先新增迁移（保持单 head）。只有阻断全新安装、且后置迁移无法修复到达该点之前的失败时，才允许经过审计的例外。

## Phase9B 运维常量漂移（验收收尾）

验收收尾发现 Phase9B 运维工具仍把 P44 `a9b8c7d6e5f4` 写成当前 head。这是运维安全门常量过期，不是业务/模型/迁移语义缺陷。

已同步（显式固定值，不改为运行时动态接受任意 head）：

- `phase9b_migration/migrate.py`：`ALEMBIC_HEAD_EXPECTED = 4f83c9e2a1d7`（P53）
- `phase9b_migration/main.py`：`DEFAULT_DRILL_REV = 653bbcf9847b`（当前 head 的直接父版本 P52）

不得再声称 P44 是当前 head。P44 仅作为历史升级锚点保留。防复发见 `tests/test_phase9b_migration_tools.py::TestOpsHeadGate`。`phase9a/bootstrap_db.py` 的单 head 门与同一常量对齐（recovery 测试依赖它升 head）。未重跑隔离 PostgreSQL 专项，不影响此前 PG 验收结论。

## 测试命令、数量和时间

未运行：全部 evolution、Wiki 编译全量、前端测试/build、浏览器验收、真实模型。理由：本轮只验收隔离 PG 接入与共享 `business_ops` 回归；SQLite 专项不作为 PG 证据；未改前端/编译管线/模型适配。

```text
# 第一层
python tools/pg_wikiskill_acceptance.py --check
python tools/pg_wikiskill_acceptance.py --run

# 第二层（显式 URL）
python -m pytest tests/test_skill_evolution_stage8u_postgresql.py -q
# 22 passed in 32.84s（全部实际执行，无 skip）

# 第三层（一次）
python -m pytest tests/test_skill_evolution_stage8u_postgresql.py \
  tests/test_skill_evolution_stage8d.py \
  tests/test_skill_evolution_stage8k.py \
  tests/test_skill_evolution_stage8l.py \
  tests/test_skill_evolution_stage8n.py \
  tests/test_skill_evolution_stage8p.py \
  tests/test_skill_evolution_stage8t_human_labels.py \
  tests/test_skill_evolution_stage8m.py::test_p53_event_columns_migration_roundtrip \
  tests/test_skill_evolution_stage8m.py::test_single_alembic_head_p53 -q
# 86 passed in 85.72s
```

stage8d/k/l/n/p/t/m 为 SQLite 隔离或静态核验，只回归共享代码，**不是 PostgreSQL 证据**。

## 资源清理

验收完成后：

- 已保存容器日志与脱敏证据（见上报告目录）。
- 已停止并删除本任务容器 `wikiskill-pg-acceptance-20260908`。
- 已删除恢复数据库。
- 已验证 55432 不再监听。
- 已删除带本任务标签的 volume `wikiskill_pg_acceptance_20260908`。
- 未执行 `docker system prune`；未删除其他镜像/容器/volume；pgvector 镜像保留；未关闭 Docker Desktop。

## 边界

无生产操作、无真实模型请求、无真实凭据入库。剩余仅限：真实供应商校准；阶段 7B 效果实验。
