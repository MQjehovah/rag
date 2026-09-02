# Phase H-2 生产迁移 Runbook（最终版，待人工确认）

> 状态：**H-1 封板，本 runbook 尚未执行，等待人工批准 H-2。**
> 目标库：`backend/data/notes.db`（SQLite，当前 alembic_version = `b8e9f0a1b2c3` P31）。
> 迁移链：P31 → (stamp) P32 `c5e6f7a8b9d0` → reconciliation `a8b9c0d1e2f3` → P33 `d1e2f3a4b5c6`。
> 本迁移 **不可逆**（P33 downgrade 拒绝），恢复仅靠迁移前已验证备份。

## 统一约定（强制）

- **所有命令一律在 `backend` 目录下执行**（本文件所有命令均以此为工作目录）。
- 数据库路径一律写 `data\notes.db`（相对 `backend`，即 `backend\data\notes.db`），**不得**写 `backend\data\notes.db`。
- Python 解释器统一为 `.\.venv\Scripts\python.exe`（相对 `backend`）。
- **每次调用外部 exe（python.exe）后必须检查 `$LASTEXITCODE`，非零立即停止。**
- **PowerShell cmdlet（New-Item / Move-Item / Test-Path）统一使用 `-ErrorAction Stop` + `try/catch`，不得用 `$LASTEXITCODE` 判断 `Move-Item`。**
- **禁止 `-Force` 覆盖 quarantine 或 notes.db。**
- 所有实际 backup/quarantine 路径以 `$backupRoot`（用户提供的真实 Windows 绝对目录）为准，本文件不擅自选择或执行。

## 执行前必须由用户提供

1. 真实 Windows 绝对备份目录，例如 `D:\backups\notes-db\20260826-H2`（禁止占位符），下方统一记为 `$backupRoot`。
2. 确认停服顺序与工具（API / scheduler / source worker）。

## 前置约束

- API、scheduler、source worker 及所有 DB 写入者**未全部停下时，禁止 stamp**。
- 真实库存在 schema 漂移：P32 列和 `knowledge_debt_users` 表已由历史 `init_db`
  补出，但版本号停在 P31、三个 debt 索引缺失（users 三索引已存在）。preflight 已精确验证。
- 真实库 6 条旧债务：1 条可回填（admin scope）、4 条无 scope（隔离）、1 条空 query（隔离）。
- 生产 H-2 **必须停服**执行（Step 0），不依赖 WAL 并发一致性测试来免于停服。

## Step 0：停服 + 初始化 $backupRoot

停止 API、scheduler、source worker 及所有 DB 写入者，确认无活动写连接。

```powershell
cd backend

# ---- 用户填写唯一的 Windows 绝对 $backupRoot ----
$backupRoot = "D:\backups\notes-db\20260826-H2"   # TODO: 用户填写真实绝对路径

# ---- 校验：必须是绝对路径 ----
if (-not [System.IO.Path]::IsPathRooted($backupRoot)) {
    throw "backupRoot 必须是绝对路径：$backupRoot"
}

# ---- 校验：禁止位于项目目录 / backend\data / 真实库目录内 ----
$backendRoot = (Resolve-Path ".").Path              # backend 目录
$projectRoot = (Resolve-Path "..").Path             # 项目根
foreach ($forbidden in @($projectRoot, "$backendRoot\data")) {
    if ($backupRoot.StartsWith($forbidden, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "backupRoot 禁止位于 $forbidden 内：$backupRoot"
    }
}

# ---- 校验：$backupRoot 必须是尚不存在的新目录；已存在则直接拒绝，避免复用旧 baseline/backup-result ----
if (Test-Path -LiteralPath $backupRoot -ErrorAction Stop) {
    throw "backupRoot 已存在，拒绝复用（必须使用全新的唯一目录）：$backupRoot"
}

# ---- 安全创建 $backupRoot ----
try {
    New-Item -ItemType Directory -Path $backupRoot -ErrorAction Stop | Out-Null
} catch {
    throw "创建 backupRoot 失败：$($_.Exception.Message)"
}

# ---- 同时安全创建 quarantine 子目录 ----
$quarantineDir = Join-Path $backupRoot "quarantine"
try {
    New-Item -ItemType Directory -Path $quarantineDir -ErrorAction Stop | Out-Null
} catch {
    throw "创建 quarantineDir 失败：$($_.Exception.Message)"
}
```

## 严格迁移顺序（任一步失败即停止，不继续下一步）

### Step 1：preflight 并保存 baseline

```powershell
.\.venv\Scripts\python.exe scripts\preflight_h2.py --db data\notes.db --baseline "$backupRoot\baseline.json"
if ($LASTEXITCODE -ne 0) { throw "Step 1 preflight 失败，禁止继续" }
```

preflight 全部 PASS 才继续（版本 P31、漂移 fingerprint、行数基线、card_v3_enabled=1、无重复 key、integrity/FK）。

### Step 2：创建一致备份（输出机器可读结果，消除时间戳占位）

```powershell
.\.venv\Scripts\python.exe scripts\backup_restore.py backup data\notes.db -o "$backupRoot" --result-json "$backupRoot\backup-result.json"
if ($LASTEXITCODE -ne 0) { throw "Step 2 backup 失败，禁止继续" }

# 从 JSON 加载并固定实际备份目录（禁止搜索「最新目录」或人工替换）
$backupResult = Get-Content -LiteralPath "$backupRoot\backup-result.json" -Raw | ConvertFrom-Json
$backupDir = $backupResult.backup_dir
if ([string]::IsNullOrWhiteSpace($backupDir) -or -not (Test-Path -LiteralPath $backupDir -PathType Container -ErrorAction Stop)) {
    throw "backup 结果 JSON 无效（backup_dir 缺失或非目录）：$backupDir"
}
```

备份目录由脚本按时间戳生成并写入 `backup-result.json`（含 `backup_dir`、`manifest`、`snapshot_sha256`），已存在即拒绝覆盖。

### Step 3：恢复排练（restore rehearsal）

```powershell
.\.venv\Scripts\python.exe scripts\backup_restore.py restore "$backupDir" "$backupRoot\rehearsal\notes-restored.db"
if ($LASTEXITCODE -ne 0) { throw "Step 3 restore rehearsal 失败，禁止继续" }
```

### Step 4：恢复副本 preflight 对比

```powershell
.\.venv\Scripts\python.exe scripts\preflight_h2.py --db "$backupRoot\rehearsal\notes-restored.db" --compare "$backupRoot\baseline.json"
if ($LASTEXITCODE -ne 0) { throw "Step 4 恢复副本对比失败，禁止继续" }
```

### Step 5：stamp P32（仅在 preflight + 恢复排练全部通过后）

```powershell
.\.venv\Scripts\python.exe -m alembic -x database_url="sqlite:///./data/notes.db" stamp c5e6f7a8b9d0
if ($LASTEXITCODE -ne 0) { throw "Step 5 stamp P32 失败，禁止继续" }
```

### Step 6：upgrade reconciliation

```powershell
.\.venv\Scripts\python.exe -m alembic -x database_url="sqlite:///./data/notes.db" upgrade a8b9c0d1e2f3
if ($LASTEXITCODE -ne 0) { throw "Step 6 upgrade reconciliation 失败，禁止继续" }
```

### Step 7：postflight reconciled（含 flag 基线对比，--baseline 必填）

```powershell
.\.venv\Scripts\python.exe scripts\postflight_h2.py --db data\notes.db --stage reconciled --baseline "$backupRoot\baseline.json"
if ($LASTEXITCODE -ne 0) { throw "Step 7 postflight reconciled 失败，禁止继续" }
```

（校验：版本 a8b9c0d1e2f3、债务分类 migrated=1/no_scope=4/empty_query=1、索引定义正确、行数不变、feature flag 与 baseline 完全一致。）

### Step 8：upgrade P33

```powershell
.\.venv\Scripts\python.exe -m alembic -x database_url="sqlite:///./data/notes.db" upgrade d1e2f3a4b5c6
if ($LASTEXITCODE -ne 0) { throw "Step 8 upgrade P33 失败，禁止继续" }
```

### Step 9：postflight p33（含 flag 基线对比，--baseline 必填）

```powershell
.\.venv\Scripts\python.exe scripts\postflight_h2.py --db data\notes.db --stage p33 --baseline "$backupRoot\baseline.json"
if ($LASTEXITCODE -ne 0) { throw "Step 9 postflight p33 失败，禁止继续" }
```

（校验：版本 d1e2f3a4b5c6、旧表/字段/8 个 legacy flag 消失、非 legacy flag 及 enabled 值不变、三个 debt/users 索引定义、knowledge_debt_users 精确列集合/类型/nullable/PK、users 精确列、结构化 FK、行数不变、integrity/FK。）

### Step 10：应用 smoke test + 重启

```powershell
.\.venv\Scripts\python.exe scripts\smoke_h2.py --db data\notes.db
if ($LASTEXITCODE -ne 0) { throw "Step 10 smoke test 失败，禁止重启服务" }
```

**smoke test 全部 PASS 之前，禁止重启服务。** smoke 只读、不调用真实 LLM/Embedding/Reranker；验证应用可 import、V4 只读查询可执行、Chat/Search/Wiki/Community/Debt 路由存在、Card/KO/旧治理路由不存在。

## 失败恢复方案（Windows PowerShell）

保持停服，不直接覆盖受损库。**先隔离受损库，恢复文件校验通过后才能替换；替换前不得覆盖尚未隔离的原库。** 全部使用 Step 2 固定的 `$backupDir`。

```powershell
cd backend

# 1. 确认停服 + 无活动写连接

# 2. 将受损 notes.db 移到唯一时间戳 quarantine 路径（不删除、不覆盖、无 -Force）
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'
$quarantine = Join-Path $quarantineDir "notes.db.damaged-$ts"
if (Test-Path -LiteralPath $quarantine -ErrorAction Stop) { throw "quarantine 目标已存在，拒绝覆盖：$quarantine" }
try {
    Move-Item -Path "data\notes.db" -Destination $quarantine -ErrorAction Stop
} catch {
    throw "隔离受损库失败：$($_.Exception.Message)"
}
if (-not (Test-Path -LiteralPath $quarantine -ErrorAction Stop)) { throw "隔离受损库失败（quarantine 未生成）" }

# 3. 从已验证备份恢复到新文件（使用 Step 2 固定的 $backupDir）
.\.venv\Scripts\python.exe scripts\backup_restore.py restore "$backupDir" "data\notes.db.restored"
if ($LASTEXITCODE -ne 0) { throw "恢复失败，受损库仍隔离在 quarantine，禁止继续" }

# 4. 校验 baseline/integrity/FK（校验通过前绝不替换原库）
.\.venv\Scripts\python.exe scripts\preflight_h2.py --db "data\notes.db.restored" --compare "$backupRoot\baseline.json"
if ($LASTEXITCODE -ne 0) { throw "恢复文件校验失败，禁止替换" }

# 5. 校验通过后原子替换（原库已被隔离，目标名可安全占用；无 -Force）
if (Test-Path -LiteralPath "data\notes.db" -ErrorAction Stop) { throw "原库仍存在，尚未隔离，禁止替换" }
try {
    Move-Item -Path "data\notes.db.restored" -Destination "data\notes.db" -ErrorAction Stop
} catch {
    throw "替换失败：$($_.Exception.Message)"
}

# 6. 重启服务
```

失败则保留受损库（quarantine 路径）和备份供审计，不得删除。

## 待用户确认项

1. 真实备份目录的绝对路径（填写 Step 0 的 `$backupRoot`）。
2. 认可 reconciliation 回填策略（无 scope/空 query 隔离、不猜测）。
3. 认可 P33 不可逆方案。
4. 确认执行前已停服且无活动写连接。
