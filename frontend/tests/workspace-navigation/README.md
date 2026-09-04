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

可选环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FRONT_URL` | `http://localhost:3001` | 前端地址 |
| `MOCK_PORT` | `8001` | 隔离 mock 端口（与 acceptance 代理目标一致） |
| `CDP_PORT` | `9333` | Chrome DevTools 端口 |
| `CHROME_PATH` | Windows Chrome | Chrome/Edge 可执行文件 |
| `SHOTS_DIR` | `os.tmpdir()/wiki-workspace-nav-shots` | 截图输出目录（不入库） |

脚本自动启动/关闭 mock 子进程与 headless Chrome，退出码 0 且全部 `PASS` 即通过。

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

## 文件

- `mock-server.mjs`：内存只读 mock（认证/工作区/wiki/revision/diff/evidence + 动态延迟与错误注入）。
- `accept.mjs`：浏览器驱动验收脚本（真实点击/输入/后退/前进，不手工 pushState 制造历史）。
