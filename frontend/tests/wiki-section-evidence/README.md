# Phase 8C 章节 Evidence / 诊断 / 编译任务面板浏览器验收（可重复运行）

隔离请求 mock + headless Chrome CDP 驱动，验证：

- 按 Section 查询的章节 Evidence 抽屉（状态/截断/空/失败、分页 offset+limit、
  竞态与跨页不覆盖、隐藏来源不计 total、320px 抽屉完整可用）
- 编辑者只读诊断（按查看 revision 刷新：`?revision_id=`；skill 无 selection；
  selected_by 受限枚举中文“选择方式”；reason_code 受控映射/未知通用文案；
  validation 章节来自该 revision；无权查看的 revision → 404 态）
- 管理员“当前工作区编译任务”面板（workspace 过滤 + limit/offset 分页、
  ctxGen 上下文竞态、轮询不重叠、展开 run Stage 随真实状态刷新、retry/cancel 状态流）

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

## 覆盖场景（T 保留既有语义；V 为 8C.1 新增）

- T1 两个不同 Section 的 Evidence 不串用（各开抽屉核对不同 evidence）
- T2 空列表（“暂无可查看的章节证据”）与失败（注入 500）分开；stale/rejected/
  hash 变化（“内容已变化”）/长内容截断提示齐全；失败抽屉“重试”可恢复
- T3 抽屉请求迟到 + 跨页面切换不覆盖（mock 延迟后切页，旧内容不出现）
- T4 读者仅打开 evidence 抽屉：请求日志无 diagnostics / compile runs，DOM 无面板
- T5 编辑者（非 admin）可见 EditorDiagnostics（Skill/自动/当前 Wiki 配置/未知），
  无 Run 面板且不调用 run API
- T6 管理员按当前工作区看到 runs（首页 20 行，全量 23 条分页文案），请求带
  `workspace_id`；切销售工作区过滤正确；run 展开显示 stage 时间线；safe error 展示且不串行
- T7 retry 409 受控提示并刷新；retry 成功入队且连点只发一次 POST（attempt 递增）；
  queued→cancelled；running→cancel_requested（“取消请求中”）→ 推进后 cancelled（“已取消”）
- T8 收起面板停止轮询；轮询请求不重叠、间隔合理（mock 时间窗口检查）
- T9 结构化 endpoint 正文可见；既有编辑入口打开/取消回归
- T10 桌面(1400)与 320px 的抽屉 / 任务面板截图（打印实际路径；320 无整页横向溢出）
- V1 diagnostics 按 revision 变化刷新（同页 published→preview 真实“编辑→保存”模拟，
  两个 revision 校验章节不串用）；慢旧响应不覆盖新 revision
- V2 普通读者仍不请求 diagnostics；编辑器可读当前 published，草稿 revision
  diagnostics 404 时显示“无权查看该版本”且不展示章节名
- V3 选择方式中文标签映射（auto→自动/manual→人工…，无“由…选定”）；
  reason_code 受控映射（DETERMINISTIC_HIGH_CONFIDENCE / MANUAL_OVERRIDE …）与
  unknown 通用文案
- V4 Run：慢 A 加载期间切 B，B 自动加载且旧响应不覆盖；请求中收起再展开不卡住；
  展开 run Stage 随真实状态 running→succeeded 显示成功而非旧 running；retry 后
  attempt/current_stage 与 Stage 更新
- V5 分页：evidence 第二页可访问、不重复不遗漏、隐藏来源不计 total（26 底层 → 24）；
  runs 翻页正确且面板不把当前页当全量；切 Section / Workspace 后页码与内容重置
- V6 320px 抽屉：bounding rect 落在视口内（left≥0 且 right≤viewport width），
  element/full 完整 320px 截图 + 桌面截图；长内容可滚动可达
- RA1 旧 getRun 迟到不覆盖新 Stage/status/attempt（收起→重开产生新请求，旧快照迟到被丢弃；
  stageSeqMap/expandedId/ctxGen 协同）
- RA2 retry 完成时 listRuns 已在途 → 结束后补一次操作后刷新（pendingRefresh，不丢最终状态）
- RA3 单次 getRun 持续 > 轮询周期：轮询驱动详情刷新在途去重（同 run 的 getRun 在途 ≤1，
  不无界叠加且持续刷新）；慢详情下收起→重开旧响应不覆盖新结果；run 终态最终显示
  （attempt/status 正确、无残留 loading、静默期无新增 getRun）
- T11 门禁回归（以上全部通过）

## 文件

- `mock-server.mjs`：内存 mock（认证/工作区/wiki 详情含 preview/章节 evidence 分页/
  诊断按 revision/编译任务列表分页与状态流 + `__control`/`__log`/`__runs-timeline`/
  `__getrun-timeline` 动态控制；`slowRunDetailAlwaysRun`+`slowRunDetailAlwaysMs` 使指定 run
  每次 getRun 都延迟 ≥ 轮询周期，供 RA3 在途去重验收）。
- `accept.mjs`：浏览器驱动验收脚本（真实点击/输入，不手工 pushState）。
