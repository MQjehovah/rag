# Phase 9A 真实后端浏览器联调（live9a）

对真实 FastAPI 后端 + 隔离 SQLite 的浏览器端验收（CDP 驱动 headless Chrome、原生 WebSocket、
真实 DOM 点击/读取、真实 HTTP 登录）。**不 mock 被测链路**：页面与后端之间只有 vite 代理。

只读仓库代码；不启动/不停止 vite 与后端（由主 Agent orchestrator 统一管理），
仅自行启动/停止本任务的 headless Chrome。

## 前置（主 Agent orchestrator 提供）

- 后端 uvicorn 已运行于 `http://127.0.0.1:8810`（`.phase9a\<session>` 临时 SQLite：
  `python -m alembic upgrade head` + seed + 触发编译，全部 wiki 达到终态）。
  - `phase9a/CONTRACT.md` §3 冻结数据：三个账号 `phase9a-admin / phase9a-editor /
    phase9a-reader`（密码 `Phase9a!2026`），工程/销售工作区与 wiki 已 publish。
- 前端 vite 已运行于 `http://127.0.0.1:3020`，且 `/api` 代理到 `http://127.0.0.1:8810`
  （即 orchestrator 以 `VITE_DEV_PORT=3020 VITE_API_PROXY_TARGET=http://127.0.0.1:8810`
  启动；**不是** acceptance/副本模式，避免 `.env.acceptance` 的假数据与横幅）。
- Node ≥ 22（脚本使用原生 WebSocket / fetch）。
- 本机 Chrome（`CHROME_PATH` 可覆盖；默认 Windows Chrome）。

## 运行

```bash
node frontend/tests/live9a/accept.mjs
```

退出码：

- `0`：所有预期场景执行且全部通过。
- `1`：存在失败断言、驱动异常、启动失败、空结果（如后端未就绪 → 登录失败）。
- `3`：main 之外未捕获的致命异常（兜底）。

脚本不启动 mock、不写后端库、不修改仓库 `.env`。截图/日志只写入本机临时目录。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LIVE_FRONT_URL` | `http://127.0.0.1:3020` | 前端地址（真实 vite） |
| `LIVE_BE_URL` | `http://127.0.0.1:8810` | 后端地址（真实 uvicorn） |
| `LIVE_CDP_PORT` | `9666` | Chrome DevTools 调试端口（与 CONTRACT 一致） |
| `LIVE_SHOTS_DIR` | `os.tmpdir()/live9a-shots` | 截图输出目录（不入库） |
| `CHROME_PATH` | Windows Chrome | Chrome/Edge 可执行文件 |

## 覆盖场景

- L1 admin 登录后：工作区选择器列表同时含 `Phase9A 工程知识库` 与 `Phase9A 销售知识库`；
  切销售工作区可见 `Phase9A 销售流程`，切回工程工作区。
- L2 打开 default wiki `Phase9A 系统使用说明`：正文可读（非错误/404 占位）。
- L3 打开 api wiki `Phase9A 用户接口参考`：结构化呈现 `GET /v1/users` / `GET /v2/users`
  端点（含参数表 limit、响应媒体 application/json、业务错误码分区）；
  若 UI 退化为 Markdown（无 `.api-endpoint-section`）则如实记 FAIL（L3b），不假装通过。
- L4 Evidence 真实性：admin 打开 `GET /v2/audit` 章节证据 ≥1 条（真实来源可见）；
  reader 打开同一章节证据为空（`暂无可查看的章节证据`，total=0）。
- L5 admin Run 面板显示 ws-eng 编译 runs：经 HTTP 按 `trigger_object_id` 定位
  `Phase9A 故障重试主题`（w-fail）对应 run，DOM 行存在、展开可见真实 stage 时间线，
  且 attempt 与列表一致（failed → retry 后的 attempt ≥2 / 终态由后端驱动）。
- L5b editor：无 Run 面板 DOM，且 `GET /api/wiki-compile/runs` 返回 403。
- L6 reader 看不到销售库：工作区选择器与 HTTP 工作区目录均无销售库；可读工程 default
  wiki；直接 URL 打开销售工作区显示“不可访问”。

## 文件

- `accept.mjs`：浏览器驱动验收（真实 DOM/HTTP，无 mock）。执行前会先以
  `POST /api/auth/login` 换取三个账号的 Bearer token，写入 localStorage 后进入页面。
- `README.md`：本说明。

## 截图

关键步骤截图写入 `LIVE_SHOTS_DIR`（默认 `%TEMP%/live9a-shots`）：default wiki 正文、
api 结构化端点、admin/reader 证据抽屉、admin Run 面板、editor 无面板等。日志行首
`PASS/FAIL` 与 `SUMMARY ... exitCode=` 提供汇总。
