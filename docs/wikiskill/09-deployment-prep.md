# 部署与运维准备（09-deployment-prep.md）

> 覆盖 8B–8E + Grader v2 接线后的配置/安装/迁移/恢复/日志脱敏/数据保留与清理。
> 本轮只做文档与隔离 SQLite 验证；未执行生产迁移与生产启用。

## 1. 配置示例与必填项（backend/.env；全部默认安全关闭）

```dotenv
# —— 必需（既有系统）——
DATABASE_URL=sqlite:///./data/notes.db   # 或 PostgreSQL: postgresql+psycopg://...
JWT_SECRET=请填写高强度随机串

# —— WikiSkill（默认全部关闭）——
WIKISKILL_CONSOLE_ENABLED=false            # 只读控制台
WIKISKILL_EVOLUTION_ADMIN_ENABLED=false    # 运行控制/晋升写端点
WIKISKILL_EVOLUTION_ADMIN_REAL_ENABLED=false  # 真实模式执行（默认禁止）
WIKISKILL_EVOLUTION_ALLOW_SIMULATED_PROMOTION=false  # 模拟晋升旁路（仅隔离测试开）
WIKISKILL_BUSINESS_COMPILE_ENABLED=false   # 业务编译绑定注入
# WIKISKILL_CONSOLE_ROOTS={"lab":"D:/data/wiki-experiments/lab"}   # 受控实验根映射

# —— Grader v2 语义评审（独立于执行模型；未配置 real 评审一律 fail closed）——
WIKISKILL_REVIEWER_MODEL_ID=               # 评审模型（禁止沿用执行模型）
WIKISKILL_REVIEWER_API_URL=
WIKISKILL_REVIEWER_PROMPT_VERSION=         # 必须显式（绑定评审配置版本）
WIKISKILL_REVIEWER_TIMEOUT=60
WIKISKILL_REVIEWER_RETRIES=1
# 评审凭据沿用项目 LLM 凭据环境（如 LLM_API_KEY），日志永不输出
```

安全默认值说明：除既有系统必需项外，WikiSkill 全部默认关闭；real 执行需
`ADMIN_REAL_ENABLED=true` **且每次 start/resume 的显式确认参数与 run 记录一致**；
模拟晋升旁路即使误设开启，也需要隔离环境 + 请求显式 allow_simulated 且实验证据
非真实时才可能放行——生产语义（无测试配置、模拟证据）下仍被服务端各检查拒绝。

## 2. 安装、启动、健康检查与关闭说明

```powershell
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
# 迁移（顺序）：
alembic upgrade d974815b4a91     # 单头 P51；包含 P45–P51 演化链全部表与列
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 健康检查
curl http://127.0.0.1:8000/api/health        # 既有服务
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8000/api/evolution-console/status
# 前端
cd frontend && npm install && npm run dev   # http://localhost:3000
```
关闭说明：全部 WikiSkill 开关默认 false；控制台/写端点关闭 → 503 页面提示“功能未
启用”；编译注入关闭 → 零注入、旧行为不变（含缺 schema）；真实执行与业务晋升在关闭
时不可达。

## 3. 迁移顺序、兼容、备份与恢复

- 顺序：先备份 → `alembic upgrade head`（当前单头 `d974815b4a91`，P51 为新增审计表；
  生产库**未执行**，由运维在授权后执行）→ 启动校验 `missing_evolution_tables==[]`。
- 兼容：业务库无演化 schema 时：编译不注入照常；晋升/回退 503 明确报缺表；
  功能开启而 schema 缺失时编译 fail loud（绝不静默忽略绑定成功）。
- 备份：实验根整目录（skill_store.db + runs/ + snapshots/）拷贝；业务库连同演化表
  一起备份；恢复＝放回同绝对路径或更新 ROOTS 映射。
- 服务重启后的实验恢复：run 状态在持久记录；API 重启后可继续查询/操作；运行中的
  run 由独立 worker（子进程）执行，不因 API 重启中断；中断后按 orchestrator 租约
  过期由新 worker 接管，或在控制台对 paused 状态点“恢复”（不重置预算、不新建 run）。

## 4. 日志脱敏、数据保留与人工清理

- 日志不输出密钥/认证头/参考答案/候选全文；评审与执行请求记录只存结构化审计与
  usage（未知=null），正文进入快照/产物区且不参与 API 摘要。
- 保留策略：实验根与审计事件 append-only；取消/回退不删除记录；人工清理需在隔离
  验证后、按保留期限执行（本轮未提供自动删除端点，避免误删不可变证据）。

## 5. 模拟晋升旁路的部署限制（服务端强制）

- `allow_simulated=true` 只是请求意愿；服务端校验
  `WIKISKILL_EVOLUTION_ALLOW_SIMULATED_PROMOTION=false`（默认）时该请求仍被拒绝
  （“旁路未在服务端开启”）；
- 即使误设开关为 true，仍需 ① 实验证据为模拟 → 放行只发生在带显式 allow 的请求；
  ② 评分器校准状态 allow_business_promotion=true（v2 engineering_only 恒拒绝）；
  ③ 作用域 active、版本哈希完整。生产无测试数据时不可能以请求参数开启。

## 6. 隔离 SQLite 验证（实际执行）

- 迁移脚本经 alembic heads 解析为单头 P51；升级/降级脚本在隔离临时库执行验证建表、
  兼容（旧表保留）；生产库未执行。
- 旧功能兼容：关闭各开关的回归（stage8c feature gate 503、stage8d 编译 off、8b/8e
  只读可用）全部通过。
- PostgreSQL：本地无可用 PG 测试环境 → 列为外部阻塞/待验收；不以 SQLite 结果冒充
  PG 验收；需要新增安装/凭据时未擅自安装。
