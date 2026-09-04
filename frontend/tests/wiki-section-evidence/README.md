# Phase 8C 章节 Evidence / 诊断 / 编译任务面板浏览器验收（可重复运行）

隔离请求 mock + headless Chrome CDP 驱动，验证：

- 按 Section 查询的章节 Evidence 抽屉（状态/截断/空/失败、竞态与跨页不覆盖）
- 编辑者只读诊断摘要（Skill 配置 + 校验摘要；普通读者不渲染/不请求）
- 管理员“当前工作区编译任务”面板（workspace 过滤、retry/cancel 状态流、轮询不重叠）

**不连正式后端、不写数据库、不调模型**。数据与 `docs/phase-8c-contract.md` 逐字段对齐。

## 前置条件

- 前端 acceptance dev server 运行于 3001（`.env.acceptance` 把 `/api` 代理到 8001）：
  ```bash
  cd frontend
  npm run dev:acceptance
  ```
- 本机 Chrome（`CHROME_PATH` 可覆盖；默认 Windows Chrome）。
- Node ≥ 22（驱动使用原生 WebSocket）。
- 运行前 8001 / 9555 无其他占用（本 mock 默认 8001；CDP 默认 9555）。

## 运行

```bash
node frontend/tests/wiki-section-evidence/accept.mjs
```

退出码：

- `0`：所有预期场景执行且全部通过。
- `1`：存在失败断言、驱动异常、启动失败、未执行完预期场景或空结果。
- `3`：main 之外未捕获的致命异常（兜底）。

脚本自动启动/关闭 mock 子进程与 headless Chrome，汇总只输出一次
（passed / failed / executed / driverError）。资源清理不覆盖失败结果。

### 负向自测（故意失败，预期非零退出）

```bash
NEG_ASSERT=1 node frontend/tests/wiki-section-evidence/accept.mjs; echo $?
NEG_ERROR=1  node frontend/tests/wiki-section-evidence/accept.mjs; echo $?
```

### 可选环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FRONT_URL` | `http://localhost:3001` | 前端地址 |
| `MOCK_PORT` | `8001` | 隔离 mock 端口 |
| `CDP_PORT` | `9555` | Chrome DevTools 端口 |
| `SHOTS_DIR` | `os.tmpdir()/wiki-section-evidence-shots` | 截图输出目录（不入库） |
| `CHROME_PATH` | Windows Chrome | Chrome/Edge 可执行文件 |

## 覆盖场景

- T1 两个不同 Section 的 Evidence 不串用（各开抽屉核对不同 evidence）
- T2 空列表（“暂无可查看的章节证据”）与失败（注入 500）分开；stale/rejected/
  hash 变化（“内容已变化”）/长内容截断提示齐全；失败抽屉“重试”可恢复
- T3 抽屉请求迟到 + 跨页面切换不覆盖（mock 延迟后切页，旧内容不出现）
- T4 读者仅打开 evidence 抽屉：请求日志无 diagnostics / compile runs，DOM 无面板
- T5 编辑者（非 admin）可见 EditorDiagnostics（Skill/自动/当前 Wiki 配置/未知），
  无 Run 面板且不调用 run API
- T6 管理员按当前工作区看到 runs，请求带 `workspace_id`；切销售工作区过滤正确；
  run 展开显示 stage 时间线；safe error 展示且不串行
- T7 retry 409 受控提示并刷新；retry 成功入队且连点只发一次 POST；
  queued→cancelled；running→cancel_requested（“取消请求中”）→ 推进后 cancelled（“已取消”）
- T8 收起面板停止轮询；轮询请求不重叠、间隔合理（mock 时间窗口检查）
- T9 结构化 endpoint 正文可见；既有编辑入口打开/取消回归
- T10 桌面(1400)与 320px 的抽屉 / 任务面板截图（打印实际路径；320 无整页横向溢出）

## 文件

- `mock-server.mjs`：内存 mock（认证/工作区/wiki 详情/章节 evidence/诊断/编译任务
  列表与状态流 + `__control`/`__log`/`__runs-timeline` 动态控制）。
- `accept.mjs`：浏览器驱动验收脚本（真实点击/输入，不手工 pushState）。
