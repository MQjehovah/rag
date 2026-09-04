# -*- coding: utf-8 -*-
# Phase 9A 隔离真实后端联调 —— 一键 orchestrator（PowerShell 5.1+）
#
# 用途：把“mock 浏览器验收”提升为“浏览器 → 真实后端 → 临时 SQLite → 真实 v3 Pipeline”。
# 流程：建会话目录 → alembic upgrade head → seed 冻结数据 → 启动隔离后端(8810)
#       → refresh-page-dirty 触发真实编译 → w-fail 首次失败后真实 retry
#       → 启动前端 vite(3020，代理 8810) → 运行 live9a 浏览器验收 → 收尾清理。
#
# 只清理本脚本启动的进程；不修改 .env；所有产物写入 <repo>\.phase9a\<session>\（已 gitignore）。
#
# 前置：已 `npm install`；backend/.venv 存在；本机 Chrome（或设 $env:CHROME_PATH）。
# 可覆盖环境变量：PHASE9A_BE_PORT / VITE_DEV_PORT / PHASE9A_CDP_PORT / VITE_API_PROXY_TARGET / CHROME_PATH
#
# 用法：  powershell -ExecutionPolicy Bypass -File phase9a\run-live.ps1
# 退出码：0=全过；非 0=任一步失败（启动失败/断言失败/驱动异常/超时）。

param(
    [string]$SessionName = ("live-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Backend = Join-Path $Root 'backend'
$Py = Join-Path $Backend '.venv\Scripts\python.exe'
$Node = 'node'

if (-not (Test-Path $Py)) { Write-Error "缺少 backend venv python: $Py"; exit 1 }
if (-not (Get-Command $Node -ErrorAction SilentlyContinue)) { Write-Error '缺少 node'; exit 1 }

$Session = Join-Path $Root ".phase9a\$SessionName"
foreach ($d in @('db', 'logs', 'shots', 'records', 'pids')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Session $d) | Out-Null
}
$DbPath = Join-Path $Session 'db\phase9a.db'
$RecFile = Join-Path $Session 'records\calls.jsonl'
$FlagFile = Join-Path $Session 'records\fault.flag'
$BePort = if ($env:PHASE9A_BE_PORT) { $env:PHASE9A_BE_PORT } else { '8810' }
$FePort = if ($env:VITE_DEV_PORT) { $env:VITE_DEV_PORT } else { '3020' }
$CdpPort = if ($env:PHASE9A_CDP_PORT) { $env:PHASE9A_CDP_PORT } else { '9666' }
$BeUrl = "http://127.0.0.1:$BePort"
$FeUrl = "http://localhost:$FePort"

Write-Output "== Phase 9A live session: $Session"
Write-Output "== DB : $DbPath"

function FailExit([string]$msg, [int]$code) {
    Write-Output "PHASE9A_FAIL $msg"
    exit $code
}

# ---------- 1) alembic upgrade head（真实迁移，含路径守卫） ----------
Write-Output '== [1/8] alembic upgrade head ...'
& $Py (Join-Path $Backend 'phase9a\bootstrap_db.py') --db $DbPath
if ($LASTEXITCODE -ne 0) { FailExit 'bootstrap_db 失败' $LASTEXITCODE }

# ---------- 2) seed 冻结数据 ----------
Write-Output '== [2/8] seed frozen fixtures ...'
& $Py (Join-Path $Backend 'phase9a\seed.py') --db $DbPath
if ($LASTEXITCODE -ne 0) { FailExit 'seed 失败' $LASTEXITCODE }

# ---------- 3) 启动隔离后端（fault flag ON：w-fail 首次编译失败） ----------
Write-Output '== [3/8] start isolated backend ...'
Set-Content -Path $FlagFile -Value 'on'
$env:DATABASE_URL = 'sqlite:///' + (($DbPath -replace '\\', '/'))
$env:PHASE9A_SESSION_DIR = $Session
$env:PHASE9A_BE_PORT = $BePort
$env:PHASE9A_RECORD_FILE = $RecFile
$env:PHASE9A_FAULT_FLAG = $FlagFile
$env:LDAP_GROUP_MAP_WIKI_EDITOR = 'editors'
$env:LDAP_GROUP_MAP_ADMIN = ''
$env:WIKI_TOPIC_ENABLED = 'true'
$env:AUTO_DAILY_SCAN_ENABLED = 'false'
$env:AUTO_ORGANIZE_ENABLED = 'false'
$env:LLM_API_URL = ''; $env:LLM_API_KEY = ''
$env:EMBEDDING_API_URL = ''; $env:RERANKER_API_URL = ''; $env:PDF_VISION_ENABLED = 'false'
$server = Start-Process -FilePath $Py -ArgumentList 'phase9a/server.py' -WorkingDirectory $Backend `
    -RedirectStandardOutput (Join-Path $Session 'logs\server.out.log') `
    -RedirectStandardError (Join-Path $Session 'logs\server.err.log') -PassThru -WindowStyle Hidden
Set-Content -Path (Join-Path $Session 'pids\server.pid') -Value $server.Id
$beUp = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Milliseconds 1000
    try {
        Invoke-RestMethod -Uri "$BeUrl/api/auth/login" -Method POST -ContentType 'application/json' -Body '{"username":"x","password":"x"}' -TimeoutSec 2 | Out-Null
        $beUp = $true; break
    } catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) { $beUp = $true; break } }
}
if (-not $beUp) { FailExit '后端未就绪（8810 起不来）' 2 }

function Login([string]$u) {
    $b = @{ username = $u; password = 'Phase9a!2026' } | ConvertTo-Json
    return (Invoke-RestMethod -Uri "$BeUrl/api/auth/login" -Method POST -ContentType 'application/json' -Body $b).token
}
$adminTok = Login 'phase9a-admin'
$authH = @{ Authorization = "Bearer $adminTok" }

function GetRuns([string]$ws) {
    $r = Invoke-RestMethod -Uri "$BeUrl/api/wiki-compile/runs?workspace_id=$ws&limit=200" -Headers $authH
    return @($r.runs)
}
function WaitRun([string]$ws, [string]$wikiId, [string]$wantStatus, [int]$timeoutSec) {
    $dl = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $dl) {
        $hit = GetRuns $ws | Where-Object { $_.wiki_page_id -eq $wikiId -and $_.status -eq $wantStatus } | Select-Object -First 1
        if ($hit) { return $hit }
        Start-Sleep -Milliseconds 1500
    }
    return $null
}

# ---------- 4) 触发真实编译（正式入口） ----------
Write-Output '== [4/8] trigger refresh-page-dirty (real runs) ...'
Invoke-RestMethod -Uri "$BeUrl/api/wiki/refresh-page-dirty" -Method POST -Headers $authH | Out-Null
$okDefault = WaitRun 'ws-eng' 'w-eng-default' 'succeeded' 120
$okApi = WaitRun 'ws-eng' 'w-eng-api' 'succeeded' 120
$okSales = WaitRun 'ws-sales' 'w-sales-default' 'succeeded' 120
if (-not ($okDefault -and $okApi -and $okSales)) {
    FailExit ("目标 wiki 未全部 succeeded: default={0} api={1} sales={2}" -f [bool]$okDefault, [bool]$okApi, [bool]$okSales) 3
}
Write-Output "   succeeded: default + api + sales"

# ---------- 5) w-fail 首次失败（fault ON），再真实 retry ----------
Write-Output '== [5/8] w-fail failed -> retry ...'
$failRun = WaitRun 'ws-eng' 'w-fail' 'failed' 120
if (-not $failRun) { FailExit 'w-fail 未在 fault ON 下 failed' 4 }
Remove-Item $FlagFile -ErrorAction SilentlyContinue
Invoke-RestMethod -Uri "$BeUrl/api/wiki-compile/runs/$($failRun.id)/retry" -Method POST -Headers $authH | Out-Null
$retryOk = WaitRun 'ws-eng' 'w-fail' 'succeeded' 120
if (-not $retryOk) { FailExit 'w-fail retry 未 succeeded' 5 }
Write-Output "   w-fail run=$($failRun.id) attempt2 succeeded"

# ---------- 6) 启动前端 vite（3020 代理 8810） ----------
Write-Output '== [6/8] start frontend vite ...'
$env:VITE_DEV_PORT = $FePort
$env:VITE_API_PROXY_TARGET = $BeUrl
$fe = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm.cmd run dev' -WorkingDirectory (Join-Path $Root 'frontend') `
    -RedirectStandardOutput (Join-Path $Session 'logs\vite.out.log') `
    -RedirectStandardError (Join-Path $Session 'logs\vite.err.log') -PassThru -WindowStyle Hidden
Set-Content -Path (Join-Path $Session 'pids\vite.pid') -Value $fe.Id
$feUp = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    try { if ((Invoke-WebRequest -Uri $FeUrl -UseBasicParsing -TimeoutSec 2).StatusCode -ge 200) { $feUp = $true; break } } catch {}
}
if (-not $feUp) { FailExit '前端 vite 未就绪' 6 }

# ---------- 7) live9a 真实浏览器联调 ----------
Write-Output '== [7/8] run live9a browser acceptance ...'
$env:LIVE_FRONT_URL = $FeUrl
$env:LIVE_BE_URL = $BeUrl
$env:LIVE_CDP_PORT = $CdpPort
$env:LIVE_SHOTS_DIR = Join-Path $Session 'shots'
if (-not $env:CHROME_PATH) { $env:CHROME_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe' }
$suiteExit = 1
& $Node (Join-Path $Root 'frontend\tests\live9a\accept.mjs')
$suiteExit = $LASTEXITCODE
if ($suiteExit -ne 0) { FailExit 'live9a 浏览器验收失败' $suiteExit }

# ---------- 8) 收尾（只清理本脚本启动的进程） ----------
Write-Output '== [8/8] cleanup ...'
foreach ($name in @('vite', 'server')) {
    $pidFile = Join-Path $Session "pids\$name.pid"
    if (Test-Path $pidFile) {
        $pv = Get-Content $pidFile
        taskkill /PID $pv /T /F 2>$null | Out-Null
    }
}
Write-Output "PHASE9A_LIVE_OK session=$Session"
Write-Output "  前端 $FeUrl  后端 $BeUrl  截图 $(Join-Path $Session 'shots')"
exit 0
