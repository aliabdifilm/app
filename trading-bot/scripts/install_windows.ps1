<#
.SYNOPSIS
    Installs ApexAlgo: copies the MQL5 sources into the MetaTrader data folder
    and sets up the Python control server.

.DESCRIPTION
    Run from the trading-bot\scripts folder:
        powershell -ExecutionPolicy Bypass -File install_windows.ps1

    The script is safe to re-run: it overwrites the MQL5 sources and leaves an
    existing server config.json untouched.

.PARAMETER TerminalDataPath
    MetaTrader 5 data folder. Auto-detected when omitted. Find it manually via
    MetaTrader's File > Open Data Folder.

.PARAMETER SkipServer
    Install only the EA files, no Python server.
#>

param(
    [string] $TerminalDataPath = "",
    [switch] $SkipServer
)

$ErrorActionPreference = "Stop"

function Write-Step  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    ($m) { Write-Host "    $m"   -ForegroundColor Green }
function Write-Warn2 ($m) { Write-Host "    $m"   -ForegroundColor Yellow }
function Write-Err   ($m) { Write-Host "    $m"   -ForegroundColor Red }

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

Write-Host ""
Write-Host "  ApexAlgo installer" -ForegroundColor White
Write-Host "  ------------------" -ForegroundColor DarkGray
Write-Host "  project: $ProjectDir" -ForegroundColor DarkGray

# ----------------------------------------------------------------------
# 1. Locate the MetaTrader 5 data folder
# ----------------------------------------------------------------------
Write-Step "Locating the MetaTrader 5 data folder"

if (-not $TerminalDataPath) {
    $root = Join-Path $env:APPDATA "MetaQuotes\Terminal"
    if (-not (Test-Path $root)) {
        Write-Err "Not found: $root"
        Write-Err "Is MetaTrader 5 installed and has it been started at least once?"
        Write-Err "Otherwise pass the folder explicitly:"
        Write-Err "  .\install_windows.ps1 -TerminalDataPath 'C:\...\Terminal\<id>'"
        exit 1
    }

    # A real terminal profile has an MQL5 subfolder; Common and the portable
    # marker folders do not.
    $candidates = Get-ChildItem $root -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName "MQL5") } |
        Sort-Object LastWriteTime -Descending

    if ($candidates.Count -eq 0) {
        Write-Err "No terminal profile with an MQL5 folder found under $root"
        exit 1
    }

    if ($candidates.Count -gt 1) {
        Write-Warn2 "Several MetaTrader profiles found:"
        for ($i = 0; $i -lt $candidates.Count; $i++) {
            Write-Host ("      [{0}] {1}  (last used {2})" -f `
                $i, $candidates[$i].Name, $candidates[$i].LastWriteTime)
        }
        $choice = Read-Host "    Which one? (Enter = 0, the most recently used)"
        if ([string]::IsNullOrWhiteSpace($choice)) { $choice = 0 }
        $TerminalDataPath = $candidates[[int]$choice].FullName
    } else {
        $TerminalDataPath = $candidates[0].FullName
    }
}

$Mql5Dir = Join-Path $TerminalDataPath "MQL5"
if (-not (Test-Path $Mql5Dir)) {
    Write-Err "No MQL5 folder in: $TerminalDataPath"
    exit 1
}
Write-Ok "Using: $TerminalDataPath"

# ----------------------------------------------------------------------
# 2. Copy the EA sources
# ----------------------------------------------------------------------
Write-Step "Copying the Expert Advisor sources"

$ExpertsTarget = Join-Path $Mql5Dir "Experts\ApexAlgo"
$IncludeTarget = Join-Path $Mql5Dir "Include\ApexAlgo"
New-Item -ItemType Directory -Force -Path $ExpertsTarget | Out-Null
New-Item -ItemType Directory -Force -Path $IncludeTarget | Out-Null

$ExpertsSource = Join-Path $ProjectDir "mql5\Experts\ApexAlgo\*"
$IncludeSource = Join-Path $ProjectDir "mql5\Include\ApexAlgo\*"

Copy-Item $ExpertsSource $ExpertsTarget -Force -Recurse
Copy-Item $IncludeSource $IncludeTarget -Force -Recurse

$expertCount  = (Get-ChildItem $ExpertsTarget -Filter *.mq5).Count
$includeCount = (Get-ChildItem $IncludeTarget -Filter *.mqh).Count
Write-Ok "$expertCount expert file(s) -> $ExpertsTarget"
Write-Ok "$includeCount include file(s) -> $IncludeTarget"

if ($includeCount -lt 12) {
    Write-Warn2 "Expected 12 include files, found $includeCount. Check the copy."
}

# ----------------------------------------------------------------------
# 3. Python control server
# ----------------------------------------------------------------------
if (-not $SkipServer) {
    Write-Step "Setting up the Python control server"

    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Warn2 "Python not found on PATH. Skipping the server."
        Write-Warn2 "Install it from python.org and tick 'Add Python to PATH',"
        Write-Warn2 "then re-run this script."
    } else {
        $version = (& python --version 2>&1)
        Write-Ok "Found $version"

        $ServerDir = Join-Path $ProjectDir "server"
        $VenvDir   = Join-Path $ServerDir ".venv"

        Push-Location $ServerDir
        try {
            if (-not (Test-Path $VenvDir)) {
                Write-Ok "Creating the virtual environment"
                & python -m venv .venv
            } else {
                Write-Ok "Reusing the existing virtual environment"
            }

            $VenvPython = Join-Path $VenvDir "Scripts\python.exe"

            Write-Ok "Installing dependencies"
            & $VenvPython -m pip install --upgrade pip --quiet
            & $VenvPython -m pip install -r requirements.txt --quiet

            Write-Ok "Installing the MetaTrader5 package (for analytics)"
            & $VenvPython -m pip install MetaTrader5 --quiet
            if ($LASTEXITCODE -ne 0) {
                Write-Warn2 "MetaTrader5 package failed to install."
                Write-Warn2 "The control server still works; only mt5_bridge.py needs it."
            }

            # Generate config.json with fresh secrets if it does not exist yet.
            $ConfigPath = Join-Path $ServerDir "config.json"
            if (-not (Test-Path $ConfigPath)) {
                Write-Ok "Generating config.json with fresh secrets"
                & $VenvPython -c "from config import Config; Config.load()"
            } else {
                Write-Ok "Keeping the existing config.json"
            }

            if (Test-Path $ConfigPath) {
                $cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
                Write-Host ""
                Write-Host "  ------------------------------------------------------------" -ForegroundColor Yellow
                Write-Host "   WRITE THESE DOWN" -ForegroundColor Yellow
                Write-Host "  ------------------------------------------------------------" -ForegroundColor Yellow
                Write-Host "   Panel password : $($cfg.panel_password)" -ForegroundColor White
                Write-Host "   EA token       : $($cfg.bot_token)" -ForegroundColor White
                Write-Host "   Panel URL      : http://$($cfg.host):$($cfg.port)" -ForegroundColor White
                Write-Host "  ------------------------------------------------------------" -ForegroundColor Yellow
                Write-Host "   The EA token goes into the EA's InpRemoteToken input." -ForegroundColor DarkGray
            }
        } finally {
            Pop-Location
        }
    }
}

# ----------------------------------------------------------------------
# 4. What to do next
# ----------------------------------------------------------------------
Write-Step "Next steps"
Write-Host @"
    1. Open MetaTrader 5, press F4 for MetaEditor.
    2. Open  Experts > ApexAlgo > ApexAlgoEA.mq5  and press F7 to compile.
       You should see: 0 errors, 0 warnings.

    3. Tools > Options > Expert Advisors:
         - tick "Allow WebRequest for listed URL"
         - add   http://127.0.0.1:8800

    4. Start the control server:
         cd "$ProjectDir\server"
         .\.venv\Scripts\Activate.ps1
         python app.py

    5. Drag ApexAlgoEA onto a chart, tick "Allow Algo Trading",
       set InpRemoteToken to the EA token above, and press OK.

    6. Open http://127.0.0.1:8800 and sign in with the panel password.

    Read docs\10-golive-checklist.md before using real money.
"@ -ForegroundColor Gray

Write-Host ""
Write-Host "  Done." -ForegroundColor Green
Write-Host ""
