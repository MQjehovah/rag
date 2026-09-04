# Phase 8B 结构化 Wiki 章节浏览器验收（可重复运行）

隔离请求 mock + headless Chrome CDP 驱动，验证 API Endpoint 章节的
结构化展示（`section_role/display` 分派、参数/响应/错误码表、折叠/键盘、
安全、320px 布局与普通 wiki 回归）。**不连正式后端、不写数据库、不调模型**。

数据与后端契约 `backend/app/core/wiki_skills/api_reference/DISPLAY_CONTRACT.md`
逐字段对齐：mock 只生成生产 DTO 白名单内的字段（`section_role` / `display` 及其子结构）。

## 前置条件

- 前端 acceptance dev server 运行于 3001（由 `.env.acceptance` 把 `/api` 代理到 8001）：
  ```bash
  cd frontend
  npm run dev:acceptance
  ```
- 本机有 Chrome（可用 `CHROME_PATH` 覆盖；默认 `C:\Program Files\Google\Chrome\Application\chrome.exe`）。
- Node ≥ 22（驱动使用原生 WebSocket）。
- 运行前 8001 / 9444 无其他占用（本 mock 默认 8001；CDP 默认 9444，避开 workspace-navigation 的 9333）。

## 运行

```bash
node frontend/tests/structured-wiki-sections/accept.mjs
```

退出码：

- `0`：所有预期场景执行且全部通过。
- `1`：存在失败断言、驱动异常、启动失败、未执行完预期场景或空结果。
- `3`：main 之外未捕获的致命异常（兜底）。

脚本自动启动/关闭 mock 子进程与 headless Chrome，汇总只输出一次
（passed / failed / executed / driverError）。清理不覆盖失败结果——退出码由执行结果决定。

### 负向自测（故意失败，预期非零退出；与正式验收分开记录）

```bash
NEG_ASSERT=1 node frontend/tests/structured-wiki-sections/accept.mjs; echo $?
NEG_ERROR=1  node frontend/tests/structured-wiki-sections/accept.mjs; echo $?
```

### 可选环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FRONT_URL` | `http://localhost:3001` | 前端地址 |
| `MOCK_PORT` | `8001` | 隔离 mock 端口 |
| `CDP_PORT` | `9444` | Chrome DevTools 端口 |
| `SHOTS_DIR` | `os.tmpdir()/structured-wiki-shots` | 截图输出目录（不入库） |
| `CHROME_PATH` | 本机 Windows Chrome | Chrome/Edge 可执行文件 |

## 覆盖场景

- T1 打开 pageA：3 个 endpoint 结构化章节（GET/POST `/users`、GET `/users/{id}`）+ overview Markdown
- T2 参数表（位置/名称/类型/必填/说明）数据准确；类型只读显式声明；`http_status` 空 → “未提供”；无虚构“默认值”列
- T3 v1 与 v2 可区分：版本标签不同，v2 有 path 参数 `userId`（string）、v1 无
- T4 折叠/展开按钮（click）与键盘 Enter（`aria-expanded`）
- T5 role=null 历史 endpoint 回退 Markdown 原文（无结构化表格元素）
- T6 人工编辑保存后 refreshDetail 重读服务端 DTO：结构消失、新正文出现
- T7 protected/manual 章节 Markdown 原文保留
- T8 折叠状态按 section 隔离：切换页面不串用，返回原页仍折叠
- T9 普通 wiki 回归：编辑 / Revision / Diff / Evidence 入口
- T10 含 `<img id="xss-leak" onerror>` 的 display 字段安全（纯文本，不执行）
- T11 1400px 与 320px 截图（打印实际路径）；320 表格局部滚动、无整页横向溢出
- T12 退出码门禁回归
- T13 8B.1：参数类型只读、响应双媒体保留、空 schema 保守文案（“未提供具体结构”，不宣称“无 Schema”）、
  example.description 可见；未知版本/缺 endpoint/parameters 非数组/required 字符串 → 整节回退 Markdown
  且同页其它合法章节正常
- T14 桌面与 320px 的“参数表 / 响应表”元素截图（长路径、双媒体），320px 长示例组件内滚动、无整页溢出

## 文件

- `mock-server.mjs`：内存 mock（认证/工作区/wiki 列表/详情/revision/diff/evidence/
  section PATCH）；PATCH 后该 section 变 `content_origin=manual`、`locked=true`、
  `section_role=null`、`display=null`，正文用提交内容——模拟真实后端人工编辑后结构消失。
- `accept.mjs`：浏览器驱动验收脚本（真实点击/输入/键盘，不手工 pushState）。
