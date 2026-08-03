param(
    [string]$SourceEnv = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $repoRoot
$targetEnv = Join-Path $repoRoot "backend\.env"
$exampleEnv = Join-Path $repoRoot "backend\.env.example"

if ([string]::IsNullOrWhiteSpace($SourceEnv)) {
    $SourceEnv = Join-Path $workspaceRoot ".env"
}

if (-not (Test-Path -LiteralPath $SourceEnv)) {
    throw "Source environment file not found: $SourceEnv"
}
if (-not (Test-Path -LiteralPath $exampleEnv)) {
    throw "Backend environment example not found: $exampleEnv"
}
if (-not (Test-Path -LiteralPath $targetEnv)) {
    Copy-Item -LiteralPath $exampleEnv -Destination $targetEnv
}

$localOverrides = @(
    "DATABASE_URL",
    "EMBEDDING_API_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS"
)

function Read-EnvMap {
    param([Parameter(Mandatory = $true)][string]$Path)

    $result = [ordered]@{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") {
            $result[$matches[1]] = $matches[2].Trim()
        }
    }
    return $result
}

$source = Read-EnvMap -Path $SourceEnv
$target = Read-EnvMap -Path $targetEnv
$example = Read-EnvMap -Path $exampleEnv
$supportedKeys = @($example.Keys)
$mergedCount = 0

foreach ($key in $supportedKeys) {
    if (-not $source.Contains($key)) {
        continue
    }
    if ($key -in $localOverrides -and $target.Contains($key) -and
        -not [string]::IsNullOrWhiteSpace($target[$key])) {
        continue
    }

    $target[$key] = $source[$key]
    $mergedCount += 1
}

$output = foreach ($line in Get-Content -LiteralPath $exampleEnv -Encoding UTF8) {
    if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") {
        $key = $matches[1]
        if ($target.Contains($key)) {
            "$key=$($target[$key])"
        }
        else {
            $line
        }
    }
    else {
        $line
    }
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($targetEnv, [string[]]$output, $utf8NoBom)

Write-Host "Project settings merged into backend/.env."
Write-Host "Merged keys: $mergedCount"
Write-Host "Preserved tested local overrides: $($localOverrides -join ', ')"
Write-Host "Comments and grouping were refreshed from backend/.env.example."
Write-Host "No configuration values were printed."
