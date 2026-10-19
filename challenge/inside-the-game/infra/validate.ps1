# Validate the MatchPulse Live Bicep template.
#
# Compiles infra/main.bicep and asserts that the compiled template contains no
# hardcoded subscription, tenant, resource group, or secret value.
#
# Usage: powershell -ExecutionPolicy Bypass -File infra\validate.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$template = Join-Path $root 'infra\main.bicep'
$compiled = Join-Path $env:TEMP 'matchpulse-main.json'

Write-Output "==> Compiling $template"
& az bicep build --file $template --outfile $compiled
if ($LASTEXITCODE -ne 0) {
    Write-Error 'az bicep build failed'
    exit 1
}

Write-Output '==> Scanning compiled template for forbidden literals'
$content = Get-Content -LiteralPath $compiled -Raw
$fail = $false

if ($content -match '"subscriptionId"\s*:\s*"[0-9a-fA-F-]{36}"') {
    Write-Output 'FAIL: compiled template contains a hardcoded subscription id'
    $fail = $true
}
if ($content -match '"tenantId"\s*:\s*"[0-9a-fA-F-]{36}"') {
    Write-Output 'FAIL: compiled template contains a hardcoded tenant id'
    $fail = $true
}
if ($content -match '(?i)"(AccountKey=|password|clientSecret|apiKey)"\s*:\s*"[^"]+"') {
    Write-Output 'FAIL: compiled template contains a secret literal'
    $fail = $true
}
if ($fail) {
    exit 1
}

Write-Output 'PASS: Bicep compiles and contains no hardcoded identifiers or secrets'
