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
| `backend/tests/fixtures/api_reference/phase9a_users_api.json` | API OpenAPI fixture（/v1、/v2 两版本端点） |
| `frontend/src/components/wiki/AdminRunsPanel.vue` | 慢详情最小修复：同 run getRun 在途去重 + 待刷新标记 |
| `frontend/tests/wiki-section-evidence/` | 新增 RA3（慢详情持续反例）；RA1/RA2 保持 |
| `frontend/tests/live9a/` | 真实后端浏览器联调（CDP，无 mock） |

## 门禁（本阶段实际执行结果，2026-09-04）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 后端集成测试 | `backend\.venv\Scripts\python.exe -m pytest tests/test_phase9a_integration.py -q` | 7 passed，退出码 0（复跑稳定） |
| 受影响 mock 套件（含 RA3） | `npm run dev:acceptance` + `node frontend/tests/wiki-section-evidence/accept.mjs` | 44/44 passed，退出码 0 |
| 前端构建 | `npm run build`（vue-tsc + vite） | 成功，退出码 0 |
| 真实后端浏览器联调 | `phase9a/run-live.ps1`（live9a） | 15/15 passed，退出码 0 |

说明：
- 首次跑受影响套件曾出现 V4c/V4d 时序抖动失败（fresh vite 首编），重跑 44/44 全绿，判定非回归。
- 隔离后端启动发现既有 main.py startup 的 `logging` 局部名 UnboundLocalError（被自身 except
  吞掉导致 `start_worker()` 不执行）——server.py 先显式启动幂等 worker 泵，未改既有源码，已上报。

## 复用同一会话重新跑浏览器验收

后端与编译已就绪时，可直接：
```powershell
$env:LIVE_FRONT_URL='http://localhost:3020'; $env:LIVE_BE_URL='http://127.0.0.1:8810'
$env:LIVE_SHOTS_DIR='...'; $env:CHROME_PATH='C:\Program Files\Google\Chrome\Application\chrome.exe'
node frontend\tests\live9a\accept.mjs
```
退出码 0=全过，1=断言/驱动/启动失败，3=致命异常。
