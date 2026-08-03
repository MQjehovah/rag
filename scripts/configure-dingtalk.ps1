param(
    [string]$SourceEnv = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repoRoot "backend\.env"
$exampleEnv = Join-Path $repoRoot "backend\.env.example"

if (-not (Test-Path -LiteralPath $envFile)) {
    if (-not (Test-Path -LiteralPath $exampleEnv)) {
        throw "Environment example not found: $exampleEnv"
    }
    Copy-Item -LiteralPath $exampleEnv -Destination $envFile
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $lines = Get-Content -LiteralPath $envFile -Encoding UTF8
    $replacement = "$Key=$Value"
    $matched = $false
    $updated = foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Key))=") {
            $matched = $true
            $replacement
        }
        else {
            $line
        }
    }
    if (-not $matched) {
        $updated += $replacement
    }
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($envFile, [string[]]$updated, $utf8NoBom)
}

if ($SourceEnv) {
    if (-not (Test-Path -LiteralPath $SourceEnv)) {
        throw "Source environment file not found: $SourceEnv"
    }

    $sourceValues = @{}
    foreach ($line in Get-Content -LiteralPath $SourceEnv -Encoding UTF8) {
        if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") {
            $sourceValues[$matches[1]] = $matches[2].Trim()
        }
    }

    $required = @(
        "DINGTALK_APP_KEY",
        "DINGTALK_APP_SECRET",
        "DINGTALK_OPERATOR_ID",
        "DINGTALK_KNOWLEDGE_BASE_ID"
    )
    $missing = @($required | Where-Object {
        -not $sourceValues.ContainsKey($_) -or
        [string]::IsNullOrWhiteSpace($sourceValues[$_])
    })
    if ($missing.Count -gt 0) {
        throw "Missing DingTalk values: $($missing -join ', ')"
    }

    $appKey = $sourceValues["DINGTALK_APP_KEY"]
    $appSecret = $sourceValues["DINGTALK_APP_SECRET"]
    $operatorId = $sourceValues["DINGTALK_OPERATOR_ID"]
    $knowledgeBaseId = $sourceValues["DINGTALK_KNOWLEDGE_BASE_ID"]
    $agentId = if ($sourceValues.ContainsKey("DINGTALK_AGENT_ID")) {
        $sourceValues["DINGTALK_AGENT_ID"]
    }
    else {
        ""
    }
}
else {
    $appKey = Read-Host "DingTalk AppKey"
    $secretSecure = Read-Host "DingTalk AppSecret" -AsSecureString
    $operatorId = Read-Host "Operator unionId"
    $knowledgeBaseId = Read-Host "Knowledge base workspace ID"
    $agentId = Read-Host "Agent ID (optional, press Enter to skip)"

    $secretPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretSecure)
    try {
        $appSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPtr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPtr)
    }
}

Set-EnvValue -Key "DINGTALK_APP_KEY" -Value $appKey.Trim()
Set-EnvValue -Key "DINGTALK_APP_SECRET" -Value $appSecret.Trim()
Set-EnvValue -Key "DINGTALK_OPERATOR_ID" -Value $operatorId.Trim()
Set-EnvValue -Key "DINGTALK_KNOWLEDGE_BASE_ID" -Value $knowledgeBaseId.Trim()
Set-EnvValue -Key "DINGTALK_AGENT_ID" -Value $agentId.Trim()

Write-Host "DingTalk settings were written to backend/.env."
Write-Host "The file is ignored by git; do not share or commit it."
