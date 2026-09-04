# Phase 8A.1 工作区导航浏览器验收（可重复运行）

隔离请求 mock + headless Chrome CDP 驱动，验证 Wiki 工作区浏览的
“统一路由导航 / 异步上下文保护”行为。**不连正式后端、不写数据库、不调模型**。

## 前置条件

- 前端 acceptance dev server 运行于 3001（由 `.env.acceptance` 把 `/api` 代理到 8001）：
  ```bash
  cd frontend
  npm run dev:acceptance
  ```
- 本机有 Chrome（可用 `CHROME_PATH` 覆盖）。
- Node ≥ 22（驱动使用原生 WebSocket）。

## 运行

```bash
node frontend/tests/workspace-navigation/accept.mjs
```

退出码：

- `0`：所有预期场景执行且全部通过。
- `1`：存在失败断言、驱动异常、启动失败、未执行完预期场景或空结果。
- `3`：main 之外未捕获的致命异常（兜底）。

脚本自动启动/关闭 mock 子进程与 headless Chrome，汇总只输出一次
（passed / failed / executed / driverError）。资源清理（关闭浏览器/终止子进程）
不覆盖失败结果——退出码由执行结果决定，而非无条件 `0`。

### 负向自测（故意失败，预期非零退出；与正式验收分开记录）

```bash
# 1) 注入失败断言 → 应返回非零
NEG_ASSERT=1 node frontend/tests/workspace-navigation/accept.mjs; echo $?

# 2) 注入驱动异常 → 应返回非零
NEG_ERROR=1 node frontend/tests/workspace-navigation/accept.mjs; echo $?
```

可选环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FRONT_URL` | `http://localhost:3001` | 前端地址 |
| `MOCK_PORT` | `8001` | 隔离 mock 端口（与 acceptance 代理目标一致） |
| `CDP_PORT` | `9333` | Chrome DevTools 端口 |
| `CHROME_PATH` | Windows Chrome | Chrome/Edge 可执行文件 |
| `SHOTS_DIR` | `os.tmpdir()/wiki-workspace-nav-shots` | 截图输出目录（不入库） |

## 覆盖场景

- R1 工作区列表首次 500 → 重试 → 完整恢复（URL 规范化 + 目录 + 详情）
- R2 无权限 workspace URL → 用户主动从选择器切到可见工作区
- R3 真实选择器 A→B，浏览器后退/前进恢复（列表不混杂）
- R4 搜索/分类同步 URL，刷新后筛选一致
- R5 应用内路由跳转到另一主题，内容同步
- R6 未保存编辑后退取消后内容/URL 一致（仅一次确认）
- R7 Revision / Diff 延迟返回，切到 B 后不得出现 A 数据
- R8 保存 A 期间切到 B，A 完成后不干扰 B 的 UI
- R9 快速连续导航，旧恢复流程不覆盖最新 Workspace
- R10 320px 打开主题 + 管理员绑定面板，无整页横向溢出
- W1 详情初次读取严格拒绝 `workspace_id` 为 other/empty/null/absent（无旧正文，固定提示）
- W2 详情刷新路径拒绝错域 `workspace_id`（清旧正文/编辑/抽屉）
- F1 阅读主题时搜索：URL 保留 pageId，目录过滤，正文保持
- F2 编辑主题时改分类：不弹确认、不丢编辑内容、pageId 保留
- F3 搜索+分类后刷新：pageId/Workspace/筛选均恢复一致

## 文件

- `mock-server.mjs`：内存只读 mock（认证/工作区/wiki/revision/diff/evidence + 动态延迟与错误注入）。
- `accept.mjs`：浏览器驱动验收脚本（真实点击/输入/后退/前进，不手工 pushState 制造历史）。
