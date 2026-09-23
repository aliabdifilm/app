<#
.SYNOPSIS
    Keeps a Windows laptop awake so the ApexAlgo EA keeps trading without a VPS.

.DESCRIPTION
    A sleeping laptop is a stopped bot: MetaTrader freezes, trailing stops stop
    moving, and no new trades open. (Server-side stop losses on open positions
    still work - the broker holds those - but everything else pauses.)

    Instead of editing your current power plan, this creates a separate
    "ApexAlgo Trading" plan copied from it and switches to that. While plugged
    in, the copy never sleeps, never hibernates, and ignores the lid being
    closed. The screen may still turn off after 30 minutes; that does not
    affect MetaTrader.

    -Revert switches back to the exact plan you had before and deletes the
    copy, so nothing is left changed.

    Settings apply on AC power only. Keep the laptop plugged in while trading.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File laptop_keep_awake.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File laptop_keep_awake.ps1 -Revert
#>

param([switch] $Revert)

$ErrorActionPreference = "Stop"

$PlanName  = "ApexAlgo Trading"
$StateDir  = Join-Path $env:LOCALAPPDATA "ApexAlgo"
$StateFile = Join-Path $StateDir "previous_power_scheme.txt"
$GuidRx    = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

# powercfg output is translated into the Windows display language, so labels
# cannot be matched. GUIDs are the same in every language; match those, and
# find our own plan by the name we gave it.
function Get-ActiveSchemeGuid {
    $out = (powercfg /getactivescheme) -join " "
    if ($out -match $GuidRx) { return $Matches[0] }
    throw "Could not read the active power plan."
}

function Get-ApexSchemeGuid {
    foreach ($line in (powercfg /list)) {
        if ($line -like "*($PlanName)*" -and $line -match $GuidRx) { return $Matches[0] }
    }
    return $null
}

# ----------------------------------------------------------------------
if ($Revert) {
    $apex = Get-ApexSchemeGuid
    if (-not (Test-Path $StateFile)) {
        Write-Host "Nothing to revert: no saved previous plan." -ForegroundColor Yellow
        if ($apex) { Write-Host "An '$PlanName' plan exists; pick another plan in Control Panel > Power Options, then delete it." }
        exit 0
    }
    $previous = (Get-Content $StateFile -Raw).Trim()
    powercfg /setactive $previous
    if ($apex) { powercfg /delete $apex }
    Remove-Item $StateFile -Force
    Write-Host "Restored your previous power plan ($previous)." -ForegroundColor Green
    Write-Host "The laptop will sleep normally again."
    exit 0
}

# ----------------------------------------------------------------------
$active = Get-ActiveSchemeGuid
$apex   = Get-ApexSchemeGuid

if (-not $apex) {
    $out = (powercfg /duplicatescheme $active) -join " "
    if ($out -notmatch $GuidRx) { throw "Could not create the '$PlanName' power plan." }
    $apex = $Matches[0]
    powercfg /changename $apex $PlanName "Keeps the laptop awake for ApexAlgo. Revert with laptop_keep_awake.ps1 -Revert"
}

# Remember the plan to go back to - but never record our own plan as the
# "previous" one, or a second run would make -Revert a no-op.
if ($active -ne $apex) {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    Set-Content -Path $StateFile -Value $active -Encoding ASCII
}

# 0 = never / do nothing
powercfg /setacvalueindex $apex SUB_SLEEP   STANDBYIDLE   0
powercfg /setacvalueindex $apex SUB_SLEEP   HIBERNATEIDLE 0
powercfg /setacvalueindex $apex SUB_BUTTONS LIDACTION     0
powercfg /setacvalueindex $apex SUB_VIDEO   VIDEOIDLE     1800
powercfg /setactive $apex

Write-Host ""
Write-Host "  Laptop will stay awake while plugged in." -ForegroundColor Green
Write-Host "  ----------------------------------------"
Write-Host "  Sleep          : never"
Write-Host "  Hibernate      : never"
Write-Host "  Closing the lid: does nothing"
Write-Host "  Screen         : turns off after 30 min (MetaTrader keeps running)"
Write-Host ""
Write-Host "  Keep it PLUGGED IN - these settings apply on AC power only." -ForegroundColor Yellow
Write-Host "  To undo:  powershell -ExecutionPolicy Bypass -File laptop_keep_awake.ps1 -Revert"
Write-Host ""
