<#
.SYNOPSIS
    FC27 Pro Clubs - Windows host capture agent installer.

.DESCRIPTION
    Installs Tesseract OCR and the VC++ runtime (if missing), creates
    host_agent\.venv, installs host_agent\requirements_windows.txt, writes
    host_agent\.env (never overwritten) and creates launcher .bat files in
    the repository root. Safe to re-run.

    All paths are derived from the location of this script, so the current
    directory of the caller does not matter.

.PARAMETER TesseractSha256
    Expected SHA-256 of the Tesseract installer at -TesseractUrl.

.PARAMETER VcRedistSha256
    Expected SHA-256 of the VC++ redistributable at -VcRedistUrl.

.PARAMETER AllowUnverifiedDownload
    Run a downloaded installer even though no expected hash was supplied.
    Without this switch, an installer with no expected hash is not run.

.EXAMPLE
    .\host_agent\install.ps1 -TesseractSha256 <hash> -VcRedistSha256 <hash>
#>
[CmdletBinding()]
param(
    # Fill in after checking the publisher's page, or pass on the command line.
    [string] $TesseractSha256 = '',
    [string] $VcRedistSha256  = '',
    [string] $TesseractUrl = 'https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.4.0.20240606.exe',
    [string] $VcRedistUrl  = 'https://aka.ms/vs/17/release/vc_redist.x64.exe',
    [switch] $AllowUnverifiedDownload
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProgressPreference = 'SilentlyContinue'

# ---- Paths: everything is built from the script's own location -------------
$HostAgentDir    = $PSScriptRoot
$RepoRoot        = Split-Path -Parent $HostAgentDir
$VenvDir         = Join-Path $HostAgentDir '.venv'
$VenvPython      = Join-Path $VenvDir 'Scripts\python.exe'
$RequirementsTxt = Join-Path $HostAgentDir 'requirements_windows.txt'
$EnvFile         = Join-Path $HostAgentDir '.env'
$TesseractDir    = Join-Path $env:ProgramFiles 'Tesseract-OCR'
$TesseractExe    = Join-Path $TesseractDir 'tesseract.exe'

function Write-Step([string] $Message) { Write-Host $Message -ForegroundColor Yellow }
function Write-Ok([string] $Message)   { Write-Host "      $Message" -ForegroundColor Green }

function Invoke-Native {
    # Run a native command and stop if it returns a non-zero exit code.
    param([Parameter(Mandatory)][string] $FilePath, [string[]] $Arguments = @())
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "'$FilePath $($Arguments -join ' ')' failed with exit code $LASTEXITCODE"
    }
}

function Install-VerifiedDownload {
    # Download an installer, verify its SHA-256, then run it.
    param(
        [Parameter(Mandatory)][string] $Url,
        [Parameter(Mandatory)][string] $OutFile,
        [string] $ExpectedSha256,
        [string[]] $InstallerArgs = @()
    )
    if (-not $ExpectedSha256 -and -not $AllowUnverifiedDownload) {
        throw ("No expected SHA-256 configured for $Url. Pass the matching -*Sha256 " +
               "parameter, or -AllowUnverifiedDownload to accept the risk.")
    }
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    $actual = (Get-FileHash -Path $OutFile -Algorithm SHA256).Hash
    if ($ExpectedSha256) {
        if ($actual -ne $ExpectedSha256.Trim()) {   # -ne is case-insensitive
            Remove-Item -Path $OutFile -Force
            throw "SHA-256 mismatch for $Url (expected $ExpectedSha256, got $actual). Installer deleted."
        }
        Write-Ok "SHA-256 verified"
    } else {
        Write-Warning "Running UNVERIFIED installer (SHA-256 $actual)"
    }
    $proc = Start-Process -FilePath $OutFile -ArgumentList $InstallerArgs -Wait -PassThru
    if ($proc.ExitCode -notin 0, 3010) {   # 3010 = success, reboot required
        throw "Installer $OutFile exited with code $($proc.ExitCode)"
    }
}

function Add-UserPathEntry {
    # Persist a directory on the user PATH once, and update this session.
    param([Parameter(Mandatory)][string] $Directory)
    $norm    = $Directory.TrimEnd('\')
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $current) { $current = '' }
    $entries = @($current -split ';' | Where-Object { $_ })
    if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $norm })) {
        $new = (@($entries) + $norm) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $new, 'User')
        Write-Ok "Added $norm to user PATH"
    } else {
        Write-Ok "$norm already on user PATH"
    }
    if (-not (@($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $norm })) {
        $env:Path += ";$norm"
    }
}

function Write-TextFile([string] $Path, [string] $Content, [string] $EncodingName) {
    $enc = if ($EncodingName -eq 'ascii') { New-Object Text.ASCIIEncoding }
           else { New-Object Text.UTF8Encoding($false) }
    [IO.File]::WriteAllText($Path, $Content, $enc)
}

Write-Host ''
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host '  FC27 East Coasters FC - Windows Host Capture Agent Installer'   -ForegroundColor Cyan
Write-Host '  Club ID: 330323 | batmanL8 (Commissioner)'                      -ForegroundColor Cyan
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host "  Repo root: $RepoRoot"
Write-Host ''

# ---- Step 1: Python ---------------------------------------------------------
Write-Step '[1/7] Checking Python version...'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python not found. Install Python 3.11+ from https://python.org'
}
$pyVersion = (& python --version 2>&1) -join ' '
if ($LASTEXITCODE -ne 0) { throw "python --version failed: $pyVersion" }
if ($pyVersion -notmatch 'Python (\d+)\.(\d+)') { throw "Unrecognised Python version: $pyVersion" }
if ([int]$Matches[1] -lt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -lt 11)) {
    throw "Python 3.11+ required, found: $pyVersion"
}
Write-Ok "Found: $pyVersion"

# ---- Step 2: Tesseract OCR --------------------------------------------------
Write-Step '[2/7] Checking Tesseract OCR...'
if (-not (Test-Path $TesseractExe)) {
    Write-Step '      Tesseract not found. Downloading installer...'
    $installer = Join-Path $env:TEMP 'tesseract-installer.exe'
    try {
        Install-VerifiedDownload -Url $TesseractUrl -OutFile $installer `
            -ExpectedSha256 $TesseractSha256 -InstallerArgs @('/S')
    } finally {
        Remove-Item -Path $installer -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $TesseractExe)) {
        throw "Tesseract install finished but $TesseractExe is missing. Install manually: https://github.com/UB-Mannheim/tesseract/wiki"
    }
    Write-Ok 'Tesseract installed'
} else {
    Write-Ok 'Tesseract already installed'
}
Add-UserPathEntry -Directory $TesseractDir

# ---- Step 3: Visual C++ Redistributable ------------------------------------
Write-Step '[3/7] Checking Visual C++ Redistributable...'
$vcRedist = @(Get-ItemProperty 'HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' `
        -ErrorAction SilentlyContinue |
    Where-Object { $_.PSObject.Properties['DisplayName'] -and $_.DisplayName -like '*Visual C++*2022*' })
if ($vcRedist.Count -eq 0) {
    Write-Step '      Downloading VC++ Redistributable...'
    $vcFile = Join-Path $env:TEMP 'vc_redist.x64.exe'
    try {
        Install-VerifiedDownload -Url $VcRedistUrl -OutFile $vcFile `
            -ExpectedSha256 $VcRedistSha256 -InstallerArgs @('/quiet', '/norestart')
    } finally {
        Remove-Item -Path $vcFile -Force -ErrorAction SilentlyContinue
    }
    Write-Ok 'VC++ Redistributable installed'
} else {
    Write-Ok 'VC++ Redistributable already installed'
}

# ---- Step 4: virtual environment -------------------------------------------
Write-Step '[4/7] Creating Python virtual environment...'
if (Test-Path $VenvPython) {
    Write-Ok 'Virtual environment already exists'
} else {
    if (Test-Path $VenvDir) {
        Write-Warning "Incomplete venv at $VenvDir; recreating"
        Remove-Item -Path $VenvDir -Recurse -Force
    }
    Invoke-Native -FilePath 'python' -Arguments @('-m', 'venv', $VenvDir)
    Write-Ok 'Virtual environment created'
}

# ---- Step 5: Python packages -----------------------------------------------
Write-Step '[5/7] Installing Python packages...'
Invoke-Native -FilePath $VenvPython -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet')
& $VenvPython -m pip install -r $RequirementsTxt --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Install failed. Retrying without dxcam (mss fallback), same requirements list...'
    $fallback = Join-Path $env:TEMP 'fc27_requirements_nodxcam.txt'
    try {
        Get-Content $RequirementsTxt | Where-Object { $_ -notmatch '^\s*dxcam\b' } |
            Set-Content -Path $fallback -Encoding ASCII
        Invoke-Native -FilePath $VenvPython -Arguments @('-m', 'pip', 'install', '-r', $fallback, '--quiet')
    } finally {
        Remove-Item -Path $fallback -Force -ErrorAction SilentlyContinue
    }
}
Write-Ok 'Python packages installed'

# ---- Step 6: .env -----------------------------------------------------------
Write-Step '[6/7] Creating host agent .env file...'
if (-not (Test-Path $EnvFile)) {
    $envText = @"
# FC27 East Coasters FC - Windows Host Capture Agent Config
# Generated by install.ps1. Replace CHANGE_ME_REDIS_PASSWORD with the Redis password.

FC27_CLUB_ID=330323
FC_PLATFORM=common-gen5
REDIS_URL=redis://:CHANGE_ME_REDIS_PASSWORD@127.0.0.1:6379/0
CAPTURE_FPS=2
DISPLAY_W=1920
DISPLAY_H=1080
LOG_LEVEL=INFO

# Tesseract
TESSERACT_CMD=$TesseractExe
TESSDATA_PREFIX=$(Join-Path $TesseractDir 'tessdata')
"@
    Write-TextFile -Path $EnvFile -Content $envText -EncodingName 'utf8'
    Write-Ok ".env created at $EnvFile (edit REDIS_URL password)"
} else {
    Write-Ok '.env already exists - not modified'
    if (-not (Select-String -Path $EnvFile -Pattern '^\s*TESSERACT_CMD\s*=' -Quiet)) {
        Add-Content -Path $EnvFile -Value "TESSERACT_CMD=$TesseractExe"
        Write-Ok 'Appended missing TESSERACT_CMD to existing .env'
    }
}

# ---- Step 7: launchers ------------------------------------------------------
# Launchers live in the repo root; "cd /d %~dp0" keeps that as the start dir.
Write-Step '[7/7] Creating launch scripts...'
$startBat = @'
@echo off
title FC27 East Coasters FC - Capture Agent
cd /d "%~dp0"
call "%~dp0host_agent\.venv\Scripts\activate.bat"
echo.
echo ================================================================
echo   FC27 Capture Agent - East Coasters FC (batmanL8)
echo   Keep Xbox Remote Play open and FULL SCREEN before starting
echo ================================================================
echo.
python "%~dp0host_agent\capture_agent.py" %*
pause
'@
$calibrateBat = @'
@echo off
title FC27 Capture Agent - ROI Calibration
cd /d "%~dp0"
call "%~dp0host_agent\.venv\Scripts\activate.bat"
echo Running calibration mode - ROI overlays will appear on screen...
python "%~dp0host_agent\capture_agent.py" --calibrate
pause
'@
Write-TextFile -Path (Join-Path $RepoRoot 'start_capture_agent.bat') -Content ($startBat -replace "`r?`n", "`r`n") -EncodingName 'ascii'
Write-TextFile -Path (Join-Path $RepoRoot 'calibrate_capture.bat')   -Content ($calibrateBat -replace "`r?`n", "`r`n") -EncodingName 'ascii'
Write-Ok 'Launch scripts created'

# ---- Summary ----------------------------------------------------------------
Write-Host ''
Write-Host '================================================================' -ForegroundColor Green
Write-Host '  Installation Complete!' -ForegroundColor Green
Write-Host '================================================================' -ForegroundColor Green
Write-Host ''
Write-Host '  NEXT STEPS:' -ForegroundColor Cyan
Write-Host '  0. Edit host_agent\.env and set the real Redis password in REDIS_URL' -ForegroundColor White
Write-Host '  1. Start Docker stack (Redis is bound to 127.0.0.1):' -ForegroundColor White
Write-Host '     docker compose --env-file .env up -d redis fc27-mcp' -ForegroundColor Gray
Write-Host '  2. Open Xbox app > Remote Play > Your Xbox; press Win+Shift+Enter for full screen' -ForegroundColor White
Write-Host '  3. Calibrate ROIs (first time only): double-click calibrate_capture.bat' -ForegroundColor White
Write-Host '  4. Start capture agent: double-click start_capture_agent.bat' -ForegroundColor White
Write-Host '  5. Dashboards: http://localhost:3000 (Langfuse), ws://localhost:8765 (events)' -ForegroundColor White
Write-Host ''
