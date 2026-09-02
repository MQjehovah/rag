# P14 灰度切换与回滚手册

## 目标

在不删除旧 API、旧映射和已入库知识的前提下，将默认入口切换为统一数据源，并保留一键无损回滚能力。

## 部署前

1. 执行 `alembic upgrade head`，确认版本为 `f0a1b2c3d4e5`。
2. API 与 `source-worker` 必须同时运行；Docker Compose 已包含二者。
3. 目标知识库必须绑定权限组。未绑定权限组时，Worker 按 fail-closed 原则拒绝同步。
4. Token 只通过环境变量或 Secret Manager 注入，不写入连接的 `config_json`。

## 灰度顺序

1. 在“系统工具 → Feature Flag”开启 `source_hub_enabled` 与 `dingtalk_connector_enabled`。
2. 暂不关闭 `legacy_search_visible`、`legacy_ko_visible`，旧钉钉 API 继续保留。
3. 创建钉钉连接，绑定受限知识库；`config_json.max_items` 设为 `10`。
4. 触发同步。新旧入口有共享锁，同一时刻只能运行一个钉钉任务。
5. 开启 `gitlab_connector_enabled`，GitLab 连接的 `max_items` 不得大于 `50`。
6. 数据核对后再开启 `source_card_compile_enabled`。数据源只生成 Evidence 和 draft Card Proposal，不直接发布。
7. 连续观察 7 天。在“系统工具 → 上线切换”查看重复、ACL、Page 映射、失败任务等门禁。
8. 全部通过后输入确认文本“切换到统一数据源”。系统原子隐藏旧 Search/KO 导航并写入审计事件。

## 回滚

在“系统工具 → 上线切换”输入“回滚到旧入口”：

- Worker 立即停止抢新任务；
- queued 任务改为 cancelled；
- running 任务设置 `cancel_requested`，在下一安全检查点停止；
- 旧 Search、旧 KO 和旧钉钉弹窗恢复；
- Page、Evidence、Card、SourceItem、SyncRun、错误记录和映射表全部保留；
- 每次切换/回滚写入 `p14_rollout_events`。

禁止通过删除数据库记录“回滚”，也禁止在旧钉钉任务运行时启动统一入口任务。
