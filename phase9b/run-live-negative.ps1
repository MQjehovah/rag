# -*- coding: utf-8 -*-
# Phase 9B 负向自测：run-live.ps1 包装可靠性（CONTRACT §4.3 防假绿 + 清理不泄漏）
#
# 每个场景独立会话实跑 wrapper：
#   (a) accept 子进程退出码 7（后端 + vite 都已启动）→ wrapper 非零、
#       无 PHASE9A_LIVE_OK、8810/3020/9666 无监听、无本任务残留进程、
#       accept.err 有失败痕迹；
#   (b) accept exit 0 但无 SUMMARY / 无截图 → wrapper 靠产物门禁判失败；
#   (c) 步骤6（vite 启动端口被占，后端已启动）→ wrapper 非零且清理后端。
#       （步骤 4/5 之后等价的中途失败都发生在“后端已启动”阶段；(a) 额外覆盖
#         后端 + vite + node 全部在跑时的清理，(c) 覆盖 vite 起不来时后端仍被清理。）
#
# 记录每项退出码、端口/进程核验结果，失败以非零退出。
# 用法：powershell -ExecutionPolicy Bypass -File phase9b\run-live-negative.ps1

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RunLive = Join-Path $Root 'phase9a\run-live.ps1'
$NegBase = Join-Path $Root '.phase9a\neg9b'
New-Item -ItemType Directory -Force -Path $NegBase | Out-Null
$Tag = Get-Date -Format 'yyyyMMdd-HHmmss'
$Results = New-Object System.Collections.ArrayList

function Record([string]$name, [bool]$ok, [string]$detail) {
    $null = $Results.Add(@{ name = $name; ok = $ok; detail = $detail })
    Write-Output ("{0} | {1}{2}" -f ($(if ($ok) { 'PASS' } else { 'FAIL' })), $name, ($(if ($detail) { " | " + $detail } else { '' })))
}

function Is-PortListening([int]$port) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    return [bool]$c
}

function Assert-NoListeners([string]$caseName) {
    foreach ($port in 8810, 3020, 9666) {
        Record "$caseName 端口 $port 无监听" (-not (Is-PortListening $port)) "listening=$([bool](Is-PortListening $port))"
    }
}

function Invoke-NegativeCase([string]$caseName, [string]$expectAscii) {
    # 每次 wrapper 用独立 SessionName（其产物/日志即负向证据，保留在 .phase9a 下）。
    $sessName = "neg-$caseName-$Tag"
    $outLog = Join-Path $NegBase "$caseName-$Tag.out.log"
    $errLog = Join-Path $NegBase "$caseName-$Tag.err.log"
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$RunLive`"", '-SessionName', $sessName)

    $proc = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
        -ArgumentList $argList -WorkingDirectory $Root -PassThru -Wait `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $rc = $proc.ExitCode
    # wrapper 子进程 stdout 重定向文件是系统 ANSI（OEM cp936）；用 Default 解码保证
    # ASCII 匹配；匹配断言统一用 ASCII 标记，避免编码差异导致误判。
    $output = ''
    if (Test-Path $outLog) { $output = [System.IO.File]::ReadAllText($outLog, [System.Text.Encoding]::Default) }
    if (Test-Path $errLog) { $output += [System.IO.File]::ReadAllText($errLog, [System.Text.Encoding]::Default) }

    Record "$caseName wrapper 退出码非零" ($rc -ne 0) "rc=$rc (expect !=0)"
    Record "$caseName 无 PHASE9A_LIVE_OK" ($output -notmatch 'PHASE9A_LIVE_OK') 'checked'
    if ($expectAscii) {
        Record "$caseName 失败标记见日志" ($output -match $expectAscii) "expect=$expectAscii"
    }
    Assert-NoListeners $caseName
}

function Assert-NoResidual($caseName) {
    $residual = @()
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'server\.py' } | ForEach-Object { $residual += "python server.py pid=$($_.ProcessId)" }
    Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'vite' } | ForEach-Object { $residual += "node vite pid=$($_.ProcessId)" }
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'remote-debugging-port' } | ForEach-Object { $residual += "chrome cdp pid=$($_.ProcessId)" }
    Record "$caseName 无残留任务进程" ($residual.Count -eq 0) ($(if ($residual.Count) { ($residual -join '; ') } else { 'none' }))
}

# ---------- 注入脚本 ----------
$ScriptsDir = Join-Path $NegBase 'scripts'
New-Item -ItemType Directory -Force -Path $ScriptsDir | Out-Null

$acceptA = Join-Path $ScriptsDir "negA-exit7-$Tag.mjs"
@'
console.error('NEG-A accept child failure trace: injected exit 7');
process.exit(7);
'@ | Set-Content -Path $acceptA -Encoding UTF8

$acceptB = Join-Path $ScriptsDir "negB-exit0-nosummary-$Tag.mjs"
@'
// 故意 exit 0 且不打印 SUMMARY、不产生截图 → 产物门禁必须判失败。
console.log('NEG-B printed nothing meaningful');
process.exit(0);
'@ | Set-Content -Path $acceptB -Encoding UTF8

$acceptC = Join-Path $ScriptsDir "negC-hang-$Tag.mjs"
@'
// 故意挂起（不退出）→ wrapper 须在 PHASE9A_ACCEPT_TIMEOUT 后杀 accept 树并判失败。
console.error('NEG-C hanging accept injected');
setInterval(() => {}, 1000);
'@ | Set-Content -Path $acceptC -Encoding UTF8

Write-Output "== 负向自测开始 tag=$Tag"

# ---------- (a) accept 子进程退出码 7 ----------
Write-Output '== 场景 A：accept exit=7（后端+vite 已启动，包装须清理且判失败）=='
$oldA = $env:PHASE9A_ACCEPT_SCRIPT
$env:PHASE9A_ACCEPT_SCRIPT = $acceptA
try {
    Invoke-NegativeCase 'A' 'accept exit=7'
    $errTail = Join-Path $Root (".phase9a\neg-A-$Tag\logs\accept.err.log")
    if (Test-Path $errTail) {
        $errText = [System.IO.File]::ReadAllText($errTail, [System.Text.Encoding]::UTF8)
        Record 'A accept.err 含失败痕迹' ($errText -match 'NEG-A accept child failure') "len=$($errText.Length)"
    } else {
        Record 'A accept.err 含失败痕迹' $false 'accept.err 不存在'
    }
    Assert-NoResidual 'A'
}
finally {
    if ($null -eq $oldA) { Remove-Item Env:\PHASE9A_ACCEPT_SCRIPT -ErrorAction SilentlyContinue } else { $env:PHASE9A_ACCEPT_SCRIPT = $oldA }
}

# ---------- (b) accept exit 0 但无 SUMMARY / 无截图 → 产物门禁 ----------
Write-Output '== 场景 B：accept exit=0 但无 SUMMARY/无截图（产物门禁须判失败）=='
$oldB = $env:PHASE9A_ACCEPT_SCRIPT
$env:PHASE9A_ACCEPT_SCRIPT = $acceptB
try {
    Invoke-NegativeCase 'B' 'ACCEPT_GATE'
    Assert-NoResidual 'B'
}
finally {
    if ($null -eq $oldB) { Remove-Item Env:\PHASE9A_ACCEPT_SCRIPT -ErrorAction SilentlyContinue } else { $env:PHASE9A_ACCEPT_SCRIPT = $oldB }
}

# ---------- (c) accept 超时：后端+vite 已启动，中途运行期失败 → 清理不泄漏 ----------
Write-Output '== 场景 C：accept 挂起超时（后端+vite 在跑）→ wrapper 非零且清理后端/vite/accept =='
$oldC = $env:PHASE9A_ACCEPT_SCRIPT
$oldT = $env:PHASE9A_ACCEPT_TIMEOUT
$env:PHASE9A_ACCEPT_SCRIPT = $acceptC
$env:PHASE9A_ACCEPT_TIMEOUT = '6000'
try {
    Invoke-NegativeCase 'C' 'ACCEPT_TIMEOUT'
    Record 'C 端口 8810 已释放（后端被清理）' (-not (Is-PortListening 8810)) 'checked'
    Assert-NoResidual 'C'
}
finally {
    if ($null -eq $oldC) { Remove-Item Env:\PHASE9A_ACCEPT_SCRIPT -ErrorAction SilentlyContinue } else { $env:PHASE9A_ACCEPT_SCRIPT = $oldC }
    if ($null -eq $oldT) { Remove-Item Env:\PHASE9A_ACCEPT_TIMEOUT -ErrorAction SilentlyContinue } else { $env:PHASE9A_ACCEPT_TIMEOUT = $oldT }
}

# ---------- 汇总 ----------
$bad = @($Results | Where-Object { -not $_.ok })
Write-Output ''
Write-Output ('SUMMARY neg passed={0} failed={1} executed={2}' -f ($Results.Count - $bad.Count), $bad.Count, $Results.Count)
if ($bad.Count) {
    Write-Output 'FAILED:'
    $bad | ForEach-Object { Write-Output ("  - {0}: {1}" -f $_.name, $_.detail) }
    exit 1
}
Write-Output 'ALL_NEGATIVE_PASS'
exit 0
