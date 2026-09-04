# Phase 9A — 隔离真实后端联调（checkpoint：4ed62fc）

把 Phase 1–8 的 mock 浏览器验收，提升为「浏览器 → 真实 FastAPI 路由/Service/ORM/ACL/
Pipeline/发布/图谱 → 隔离临时 SQLite（真实 alembic upgrade head）」。本轮不新增产品
功能、不连接真实业务库/LDAP/远程数据源/真实模型、不部署、不迁移真实数据库。

- 冻结的标识符、端口、隔离证明与替身行为：见 [CONTRACT.md](CONTRACT.md)。
- 一键复跑真实后端 + 浏览器联调：`powershell -ExecutionPolicy Bypass -File phase9a\run-live.ps1`
  （内部依次执行 alembic upgrade head → seed → 启动隔离后端(8810) → refresh-page-dirty 触发
  真实 v3 编译 → w-fail 首次失败后真实 retry → 前端 vite(3020) → live9a 浏览器验收 → 清理）。
- 所有日志/截图/数据库/调用记录/凭据仅写入 `<repo>\.phase9a\`（gitignore），不入库。

## 目录速览

| 路径 | 作用 |
|---|---|
| `phase9a/CONTRACT.md` | 主 Agent 冻结的共享契约（标识符/端口/断言/分工） |
| `phase9a/run-live.ps1` | 一键 orchestrator（真实后端 + 前端 + live9a 浏览器） |
| `backend/phase9a/bootstrap_db.py` | 路径守卫 + alembic upgrade head（单 head `a9b8c7d6e5f4`） |
| `backend/phase9a/seed.py` | 幂等 seed（users/workspaces/notebooks/pages/evidence/wikis） |
| `backend/phase9a/server.py` | 隔离后端：模型替身（含调用记录/故障注入）+ worker 泵 + uvicorn |
| `backend/phase9a/fixtures.py` | 冻结常量 + recorder/runner + seed 实现 |
| `backend/tests/test_phase9a_integration.py` | pytest 后端链路（alembic + 真实路由 + 真实 v3 pipeline） |
| `backend/tests/fixtures/api_reference/phase9a_users_api.json` | API OpenAPI fixture（/v1、/v2 不同路径 Endpoint） |
| `frontend/src/components/wiki/AdminRunsPanel.vue` | 慢详情最小修复：同 run getRun 在途去重 + 待刷新标记 |
| `frontend/tests/wiki-section-evidence/` | 新增 RA3（慢详情持续反例）；RA1/RA2 保持 |
| `frontend/tests/live9a/` | 真实后端浏览器联调（CDP，无 mock） |

## 门禁（本阶段实际执行结果，2026-09-04）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 后端集成测试 | `backend\.venv\Scripts\python.exe -m pytest tests/test_phase9a_integration.py tests/test_phase9a_startup.py -q` | 10 passed（7 集成 + 3 启动闭环），退出码 0 |
| 受影响 mock 套件（含 RA3） | `npm run dev:acceptance` + `node frontend/tests/wiki-section-evidence/accept.mjs` | 44/44 passed，退出码 0 |
| 前端构建 | `npm run build`（vue-tsc + vite） | 成功，退出码 0 |
| 真实后端浏览器联调 | `phase9a/run-live.ps1`（live9a） | 15/15 passed，退出码 0 |

说明：
- 首次跑受影响套件时 V4c/V4d 曾出现失败：状态记为「出现过、原因未证实」；后续重跑通过
  不构成「非回归」的证明，仅作为可重复通过记录，不改写为已定性结论。
- 生产启动缺陷修复：`app/main.py` 曾因函数内局部 `import logging` 遮蔽模块名，使正常路径
  在 `start_worker()` 前抛 `UnboundLocalError` 并被自身 except 吞掉（worker 泵永不启动）。
  已做最小修复（模块级 `import logging` + 移除函数内遮蔽 import），启动顺序不变。
  同时删除 `backend/phase9a/server.py` 原先「提前直接 `start_worker()`」的测试绕过——
  隔离测试后端现经真实应用 startup 启动 worker（由新增启动闭环测试与 live9a 验证）。
- 本轮源码修改范围：`backend/app/main.py`（启动缺陷最小修复）、`backend/phase9a/server.py`
  （删除绕过）、`backend/tests/test_phase9a_startup.py`（新增）、
  `backend/tests/test_phase9a_integration.py`（仅口径/命名调整）及本文档。
- API 版本展示口径：见 CONTRACT §4.1——本轮验证的是 `/v1`、`/v2`「不同路径 Endpoint 展示」；
  「同一 Endpoint 的 version_scope 隔离」在生产执行链尚未验证，留待 9B 审定。

## 复用同一会话重新跑浏览器验收

后端与编译已就绪时，可直接：
```powershell
$env:LIVE_FRONT_URL='http://localhost:3020'; $env:LIVE_BE_URL='http://127.0.0.1:8810'
$env:LIVE_SHOTS_DIR='...'; $env:CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
node frontend\tests\live9a\accept.mjs
```
退出码 0=全过，1=断言/驱动/启动失败，3=致命异常。
