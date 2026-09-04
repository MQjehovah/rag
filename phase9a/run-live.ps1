# -*- coding: utf-8 -*-
# Phase 9A 隔离真实后端联调 —— 一键 orchestrator（PowerShell 5.1+）
#
# 用途：把“mock 浏览器验收”提升为“浏览器 → 真实后端 → 临时 SQLite → 真实 v3 Pipeline”。
# 流程：建会话目录 → alembic upgrade head → seed 冻结数据 → 启动隔离后端(8810)
#       → refresh-page-dirty 触发真实编译 → w-fail 首次失败后真实 retry
#       → 启动前端 vite(3020，代理 8810) → 运行 live9a 浏览器验收 → 收尾清理。
#
# Phase 9B（CONTRACT §4.3）可靠性修复：
# - 整脚本 try/finally：成功/异常/超时都必须清理本任务启动的 PID（只清本次启动的
#   server/vite/node 树；PID 归属校验 = 只 kill 本会话记录的进程对象，不读陈旧 pid 文件
#   误杀他人；清理失败不回写覆盖原失败码）。
# - step7 node 改 Start-Process -PassThru + WaitForExit（不再依赖 `& $Node` +
#   $LASTEXITCODE 陈旧值），stdout/stderr 重定向到 session logs\accept.out.log/.err.log
#   并在结束后回显尾部。
# - 产物门禁（防假绿）：node 返回 0 之外还要求 logs\accept.out.log 含
#   "SUMMARY ... exitCode=0" 且 session shots\ 至少 6 张 png；否则判失败。
# - node 调用加超时（默认 900s，可用 PHASE9A_ACCEPT_TIMEOUT 覆盖），超时杀 accept 树并失败。
# - 失败路径先回显 server/vite/accept 日志尾部再退出。
# - 支持 env PHASE9A_ACCEPT_SCRIPT 覆盖 step7 脚本（默认 frontend\tests\live9a\accept.mjs）。
#
# 只清理本脚本启动的进程；不修改 .env；所有产物写入 <repo>\.phase9a\<session>\（已 gitignore）。
#
# 前置：已 `npm install`；backend/.venv 存在；本机 Chrome（或设 $env:CHROME_PATH）。
# 可覆盖环境变量：PHASE9A_BE_PORT / VITE_DEV_PORT / PHASE9A_CDP_PORT / VITE_API_PROXY_TARGET
#                  / CHROME_PATH / PHASE9A_ACCEPT_SCRIPT / PHASE9A_ACCEPT_TIMEOUT
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

if (-not (Test-Path $Py)) { Write-Output "PHASE9A_FAIL 缺少 backend venv python: $Py"; exit 1 }
if (-not (Get-Command $Node -ErrorAction SilentlyContinue)) { Write-Output 'PHASE9A_FAIL 缺少 node'; exit 1 }

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
$AcceptScript = if ($env:PHASE9A_ACCEPT_SCRIPT) { $env:PHASE9A_ACCEPT_SCRIPT } else { Join-Path $Root 'frontend\tests\live9a\accept.mjs' }
$AcceptTimeoutMs = if ($env:PHASE9A_ACCEPT_TIMEOUT) { [int]$env:PHASE9A_ACCEPT_TIMEOUT } else { 900000 }

Write-Output "== Phase 9A live session: $Session"
Write-Output "== DB : $DbPath"

# 本会话实际启动的进程（对象级跟踪；PID 归属校验基于记录对象的 StartTime）。
$Started = New-Object System.Collections.ArrayList
$failCode = 0
$failMsg = ''

function Fail([string]$m, [int]$c) {
    $script:failCode = $c
    $script:failMsg = $m
    throw "PHASE9A_FAIL $m"
}

function Show-Tail([string]$path, [int]$lines = 25) {
    if (Test-Path $path) {
        Write-Output "----- tail: $path -----"
        Get-Content -Path $path -Tail $lines -ErrorAction SilentlyContinue | ForEach-Object { Write-Output $_ }
    }
}

function Start-Tracked([string]$name, [System.Diagnostics.Process]$proc) {
    $null = $Started.Add(@{ name = $name; proc = $proc })
    Set-Content -Path (Join-Path $Session "pids\$name.pid") -Value $proc.Id
    Write-Output "   started $name pid=$($proc.Id)"
    return $proc
}

function Stop-Tracked {
    # 只清理本会话启动的进程树；清理错误不回写覆盖原失败码。
    foreach ($entry in $Started) {
        $name = $entry.name
        $proc = $entry.proc
        try {
            if ($proc.HasExited) { continue }
            # PID 归属校验：本会话启动后若 PID 被系统回收并被其他进程复用，则存活进程的
            # StartTime 与本会话记录对象的 StartTime 相差很大 → 不 kill（不误杀他人）。
            $alive = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
            if ($alive -and $proc.StartTime -and [DateTime]::MinValue -ne $proc.StartTime) {
                $delta = [Math]::Abs(($alive.StartTime - $proc.StartTime).TotalSeconds)
                if ($delta -gt 10) {
                    Write-Output "   skip killing $name pid=$($proc.Id) (PID reused: alive StartTime=$($alive.StartTime), tracked=$($proc.StartTime))"
                    continue
                }
            }
            Write-Output "   killing $name pid=$($proc.Id)"
            taskkill /PID $proc.Id /T /F 2>$null | Out-Null
        } catch {
            Write-Output "   cleanup $name failed: $($_.Exception.Message)"
        }
    }
    $Started.Clear()
    # 孤儿的本会话 headless Chrome（accept 异常中断/超时后可能遗留）：按会话唯一标识定位，
    # 只清本会话 LIVE_SHOTS_DIR 路径 + CDP 端口的 chrome 树。
    try {
        $needle = $CdpPort
        $shotsDir = Join-Path $Session 'shots'
        $procs = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match [regex]::Escape($needle) -and $_.CommandLine -match [regex]::Escape($shotsDir) }
        foreach ($p in $procs) {
            Write-Output "   killing session chrome pid=$($p.ProcessId)"
            taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null
        }
    } catch { }
}

try {
    # ---------- 1) alembic upgrade head（真实迁移，含路径守卫） ----------
    Write-Output '== [1/8] alembic upgrade head ...'
    & $Py (Join-Path $Backend 'phase9a\bootstrap_db.py') --db $DbPath
    if ($LASTEXITCODE -ne 0) { Fail 'bootstrap_db 失败' 1 }

    # ---------- 2) seed 冻结数据 ----------
    Write-Output '== [2/8] seed frozen fixtures ...'
    & $Py (Join-Path $Backend 'phase9a\seed.py') --db $DbPath
    if ($LASTEXITCODE -ne 0) { Fail 'seed 失败' 2 }

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
    # 覆盖 backend/.env 可能存在的真实 LDAP 端点：后端子进程不得连真实 LDAP
    # （未知用户登录须立即 401；否则会触发真实 LDAP connect 超时导致“后端就绪”探测假失败）。
    $env:LDAP_SERVER_URL = ''; $env:LDAP_BIND_DN = ''; $env:LDAP_BIND_PASSWORD = ''
    $env:LDAP_USER_BASE_DN = ''; $env:LDAP_GROUP_BASE_DN = ''
    $env:LDAP_USER_FILTER = '(uid={username})'; $env:LDAP_GROUP_FILTER = '(member={user_dn})'
    $env:WIKI_TOPIC_ENABLED = 'true'
    $env:AUTO_DAILY_SCAN_ENABLED = 'false'
    $env:AUTO_ORGANIZE_ENABLED = 'false'
    $env:LLM_API_URL = ''; $env:LLM_API_KEY = ''
    $env:EMBEDDING_API_URL = ''; $env:RERANKER_API_URL = ''; $env:PDF_VISION_ENABLED = 'false'
    $env:PYTHONUNBUFFERED = '1'
    $serverProc = Start-Process -FilePath $Py -ArgumentList 'phase9a/server.py' -WorkingDirectory $Backend `
        -RedirectStandardOutput (Join-Path $Session 'logs\server.out.log') `
        -RedirectStandardError (Join-Path $Session 'logs\server.err.log') -PassThru -WindowStyle Hidden
    $null = Start-Tracked 'server' $serverProc
    $beUp = $false
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Milliseconds 1000
        try {
            $b = @{ username = 'phase9a-admin'; password = 'Phase9a!2026' } | ConvertTo-Json
            $tok = (Invoke-RestMethod -Uri "$BeUrl/api/auth/login" -Method POST -ContentType 'application/json' -Body $b -TimeoutSec 3).token
            if ($tok) { $beUp = $true; break }
        } catch { }
    }
    if (-not $beUp) { Fail '后端未就绪（8810 起不来）' 3 }

    function Login([string]$u) {
        $b = @{ username = $u; password = 'Phase9a!2026' } | ConvertTo-Json
        return (Invoke-RestMethod -Uri "$BeUrl/api/auth/login" -Method POST -ContentType 'application/json' -Body $b -TimeoutSec 15).token
    }
    $adminTok = Login 'phase9a-admin'
    $authH = @{ Authorization = "Bearer $adminTok" }

    function GetRuns([string]$ws) {
        $r = Invoke-RestMethod -Uri "$BeUrl/api/wiki-compile/runs?workspace_id=$ws&limit=200" -Headers $authH -TimeoutSec 20
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
    Invoke-RestMethod -Uri "$BeUrl/api/wiki/refresh-page-dirty" -Method POST -Headers $authH -TimeoutSec 30 | Out-Null
    $okDefault = WaitRun 'ws-eng' 'w-eng-default' 'succeeded' 120
    $okApi = WaitRun 'ws-eng' 'w-eng-api' 'succeeded' 120
    $okSales = WaitRun 'ws-sales' 'w-sales-default' 'succeeded' 120
    if (-not ($okDefault -and $okApi -and $okSales)) {
        Fail ("目标 wiki 未全部 succeeded: default={0} api={1} sales={2}" -f [bool]$okDefault, [bool]$okApi, [bool]$okSales) 4
    }
    Write-Output "   succeeded: default + api + sales"

    # ---------- 5) w-fail 首次失败（fault ON），再真实 retry ----------
    Write-Output '== [5/8] w-fail failed -> retry ...'
    $failRun = WaitRun 'ws-eng' 'w-fail' 'failed' 120
    if (-not $failRun) { Fail 'w-fail 未在 fault ON 下 failed' 5 }
    Remove-Item $FlagFile -ErrorAction SilentlyContinue
    Invoke-RestMethod -Uri "$BeUrl/api/wiki-compile/runs/$($failRun.id)/retry" -Method POST -Headers $authH -TimeoutSec 30 | Out-Null
    $retryOk = WaitRun 'ws-eng' 'w-fail' 'succeeded' 120
    if (-not $retryOk) { Fail 'w-fail retry 未 succeeded' 5 }
    Write-Output "   w-fail run=$($failRun.id) attempt2 succeeded"

    # ---------- 6) 启动前端 vite（3020 代理 8810） ----------
    Write-Output '== [6/8] start frontend vite ...'
    $env:VITE_DEV_PORT = $FePort
    $env:VITE_API_PROXY_TARGET = $BeUrl
    $feProc = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm.cmd run dev' -WorkingDirectory (Join-Path $Root 'frontend') `
        -RedirectStandardOutput (Join-Path $Session 'logs\vite.out.log') `
        -RedirectStandardError (Join-Path $Session 'logs\vite.err.log') -PassThru -WindowStyle Hidden
    $null = Start-Tracked 'vite' $feProc
    $feUp = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try { if ((Invoke-WebRequest -Uri $FeUrl -UseBasicParsing -TimeoutSec 2).StatusCode -ge 200) { $feUp = $true; break } } catch {}
    }
    if (-not $feUp) { Fail '前端 vite 未就绪' 6 }

    # ---------- 7) live9a 真实浏览器联调（Process + 退出码 + 门禁；超时可注入） ----------
    Write-Output '== [7/8] run live9a browser acceptance ...'
    $env:LIVE_FRONT_URL = $FeUrl
    $env:LIVE_BE_URL = $BeUrl
    $env:LIVE_CDP_PORT = $CdpPort
    $env:LIVE_SHOTS_DIR = Join-Path $Session 'shots'
    if (-not $env:CHROME_PATH) { $env:CHROME_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe' }

    $acceptOut = Join-Path $Session 'logs\accept.out.log'
    $acceptErr = Join-Path $Session 'logs\accept.err.log'
    # 用 System.Diagnostics.Process（经 cmd 重定向到文件）取得真实退出码并支持超时；
    # 不依赖 `& node` + $LASTEXITCODE 陈旧值（PS5.1 Start-Process 无 -Wait 时 ExitCode 为空）。
    $nodeFull = (Get-Command $Node -ErrorAction Stop).Source
    $cmdArgs = '/d /c ""' + $nodeFull + '" "' + $AcceptScript + '" 1> "' + $acceptOut + '" 2> "' + $acceptErr + '""'
    $acceptProc = New-Object System.Diagnostics.Process
    $acceptProc.StartInfo.UseShellExecute = $false
    $acceptProc.StartInfo.FileName = $env:ComSpec
    $acceptProc.StartInfo.Arguments = $cmdArgs
    $acceptProc.StartInfo.WorkingDirectory = $Root
    $acceptProc.StartInfo.CreateNoWindow = $true
    $null = $acceptProc.Start()
    $acceptProc.Refresh()
    $null = Start-Tracked 'accept' $acceptProc

    if (-not $acceptProc.WaitForExit($AcceptTimeoutMs)) {
        Fail "live9a accept 超时 ACCEPT_TIMEOUT（$($AcceptTimeoutMs / 1000)s）" 7
    }
    $suiteExit = $acceptProc.ExitCode
    Write-Output "   accept exited code=$suiteExit (script=$AcceptScript)"
    Show-Tail $acceptOut
    Show-Tail $acceptErr

    if ($suiteExit -ne 0) { Fail "live9a 浏览器验收失败（accept exit=$suiteExit）" 7 }

    # 产物门禁（防假绿）：node rc=0 之外必须 SUMMARY exitCode=0 且至少 6 张截图。
    $outText = ''
    if (Test-Path $acceptOut) { $outText = Get-Content -Path $acceptOut -Raw -ErrorAction SilentlyContinue }
    $summaryOk = $outText -match 'SUMMARY\s+.*exitCode=0'
    $shotCount = @(Get-ChildItem -Path (Join-Path $Session 'shots') -Filter '*.png' -ErrorAction SilentlyContinue).Count
    if (-not $summaryOk -or $shotCount -lt 6) {
        Fail ("产物门禁失败 ACCEPT_GATE: summaryOk={0} shots={1}（要求 ≥6 张 png 且 accept.out.log 含 SUMMARY exitCode=0）" -f $summaryOk, $shotCount) 8
    }
    Write-Output "   gate ok: summaryOk=$summaryOk shots=$shotCount"

    Write-Output '== [8/8] done'
}
catch {
    # 失败路径：先回显相关日志尾部，再进入 finally 清理（不覆盖原失败码）。
    Show-Tail (Join-Path $Session 'logs\server.out.log')
    Show-Tail (Join-Path $Session 'logs\server.err.log')
    Show-Tail (Join-Path $Session 'logs\vite.out.log')
    Show-Tail (Join-Path $Session 'logs\vite.err.log')
    Show-Tail (Join-Path $Session 'logs\accept.out.log')
    Show-Tail (Join-Path $Session 'logs\accept.err.log')
    if ($failCode -eq 0) {
        $failCode = 9
        $failMsg = "脚本异常: $($_.Exception.Message)"
    }
}
finally {
    Stop-Tracked
}

if ($failCode -ne 0) {
    Write-Output "PHASE9A_FAIL $failMsg"
    exit $failCode
}
Write-Output "PHASE9A_LIVE_OK session=$Session"
Write-Output "  前端 $FeUrl  后端 $BeUrl  截图 $(Join-Path $Session 'shots')"
exit 0
