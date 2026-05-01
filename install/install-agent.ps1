<#
.SYNOPSIS
    Installs the Claude Agent as a Scheduled Task (at startup, run as SYSTEM).

.DESCRIPTION
    - Stages the repo to $InstallDir (default C:\ClaudeAgent\app).
    - Creates a venv and installs deps + the agent package.
    - Seeds $ConfigDir\agent.toml and locks down $ConfigDir\nats.creds.
    - Registers the 'ClaudeAgent' scheduled task:
        Trigger  : At startup
        Principal: SYSTEM, highest privileges
        Action   : run-agent.cmd (wraps 'python -m agent' with log redirection)
        Restart  : 99 retries, 1 min apart, execution time unlimited
    - Removes any legacy NSSM service or prior scheduled task first.

.PARAMETER CredsFile
    Path to the Synadia creds file (.creds). Required.

.PARAMETER HostId
    Stable identifier for this machine. Required.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CredsFile,
    [Parameter(Mandatory=$true)][string]$HostId,
    [string]$NatsUrl      = "wss://connect.ngs.global:443",
    [string]$InstallDir   = "C:\ClaudeAgent\app",
    [string]$VenvDir      = "C:\ClaudeAgent\venv",
    [string]$WorkspaceDir = "C:\ClaudeAgent\workspace",
    [string]$ConfigDir    = "C:\ProgramData\ClaudeAgent",
    [string]$PythonExe    = "python",
    [string]$TaskName     = "ClaudeAgent",
    [string]$ServiceName  = "ClaudeAgent"
)

$ErrorActionPreference = "Stop"

function Require-Admin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object System.Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell (Run as Administrator)."
    }
}

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
}

Require-Admin

if (-not (Test-Path $CredsFile)) { throw "CredsFile not found: $CredsFile" }
$CredsFile = (Resolve-Path $CredsFile).Path

Write-Host "==> Preparing directories"
Ensure-Dir (Split-Path $InstallDir -Parent)
Ensure-Dir $WorkspaceDir
Ensure-Dir $ConfigDir
Ensure-Dir (Join-Path $ConfigDir "logs")

# --- Stage code -------------------------------------------------------------
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Write-Host "==> Staging code from $repoRoot to $InstallDir"
if (Test-Path $InstallDir) {
    robocopy "$repoRoot" "$InstallDir" /MIR /XD ".venv" "__pycache__" ".git" /XF "*.creds" /NFL /NDL /NJH /NJS | Out-Null
} else {
    Ensure-Dir $InstallDir
    robocopy "$repoRoot" "$InstallDir" /E /XD ".venv" "__pycache__" /XF "*.creds" /NFL /NDL /NJH /NJS | Out-Null
}
if (-not (Test-Path (Join-Path $InstallDir "agent\runner.py"))) {
    throw "Staging failed - agent\runner.py missing in $InstallDir."
}

# --- Ensure git checkout for self_update -----------------------------------
if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
    Write-Warning "install dir is not a git checkout. self_update requires git; see SETUP.md step B6."
}

# --- Create venv ------------------------------------------------------------
Write-Host "==> Creating venv at $VenvDir"
if (-not (Test-Path $VenvDir)) {
    & $PythonExe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
$venvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $venvPython)) { throw "venv python not found at $venvPython" }

Write-Host "==> Installing agent requirements"
& $venvPython -m pip install --upgrade pip | Out-Null
& $venvPython -m pip install -r (Join-Path $InstallDir "agent\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "==> Installing agent package (editable)"
& $venvPython -m pip install -e $InstallDir
if ($LASTEXITCODE -ne 0) { throw "editable install failed" }

# --- Generate the launcher wrapper with baked-in paths ---------------------
$wrapper = Join-Path $InstallDir "run-agent.cmd"
Write-Host "==> Writing launcher $wrapper"
$wrapperContent = @"
@echo off
set "AGENT_PY=$venvPython"
set "AGENT_DIR=$InstallDir"
set "LOG_DIR=$(Join-Path $ConfigDir 'logs')"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
cd /d "%AGENT_DIR%"
"%AGENT_PY%" -m agent 1>>"%LOG_DIR%\stdout.log" 2>>"%LOG_DIR%\stderr.log"
exit /b %ERRORLEVEL%
"@
[System.IO.File]::WriteAllText($wrapper, $wrapperContent, [System.Text.ASCIIEncoding]::new())

# --- Creds file -------------------------------------------------------------
$credsDest = Join-Path $ConfigDir "nats.creds"
$srcResolved = (Resolve-Path $CredsFile).Path
$dstResolved = if (Test-Path $credsDest) { (Resolve-Path $credsDest).Path } else { $credsDest }

if ($srcResolved -ieq $dstResolved) {
    Write-Host "==> Creds source is already the live creds file; just re-applying ACL"
    icacls $credsDest /inheritance:r /grant "SYSTEM:R" "Administrators:R" | Out-Null
} else {
    Write-Host "==> Installing creds to $credsDest"
    if (Test-Path $credsDest) {
        icacls $credsDest /grant "Administrators:F" | Out-Null
        Remove-Item -Force $credsDest
    }
    Copy-Item -Force $CredsFile $credsDest
    icacls $credsDest /inheritance:r /grant "SYSTEM:R" "Administrators:R" | Out-Null
}

# --- agent.toml -------------------------------------------------------------
$cfgPath = Join-Path $ConfigDir "agent.toml"
if (-not (Test-Path $cfgPath)) {
    Write-Host "==> Seeding $cfgPath from agent.toml.example"
    $example = Get-Content (Join-Path $InstallDir "agent\agent.toml.example") -Raw
    $example = $example `
        -replace 'host_id = "my-desktop"',              ("host_id = `"" + $HostId + "`"") `
        -replace 'C:/ClaudeAgent/workspace',             ($WorkspaceDir.Replace('\','/')) `
        -replace 'C:/ClaudeAgent/app',                   ($InstallDir.Replace('\','/')) `
        -replace 'C:/ClaudeAgent/venv/Scripts/python.exe', ($venvPython.Replace('\','/')) `
        -replace 'C:/ProgramData/ClaudeAgent/nats.creds',  ($credsDest.Replace('\','/')) `
        -replace 'tls://connect.ngs.global',             $NatsUrl
    [System.IO.File]::WriteAllText($cfgPath, $example, [System.Text.UTF8Encoding]::new($false))
} else {
    Write-Host "==> Keeping existing $cfgPath"
}

# --- Remove any prior service (pywin32 or NSSM) ----------------------------
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "==> Removing legacy $ServiceName service"
    Stop-Service $ServiceName -Force -ErrorAction SilentlyContinue
    $legacyNssm = "C:\ClaudeAgent\nssm.exe"
    if (Test-Path $legacyNssm) {
        & $legacyNssm remove $ServiceName confirm 2>$null | Out-Null
    }
    if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
        sc.exe delete $ServiceName | Out-Null
    }
    Start-Sleep -Seconds 1
}

# --- Remove any prior scheduled task ---------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "==> Removing existing scheduled task $TaskName"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# --- Register the scheduled task -------------------------------------------
Write-Host "==> Registering scheduled task '$TaskName'"

$action   = New-ScheduledTaskAction -Execute $wrapper
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

# --- External watchdog: separate scheduled task that probes the agent every
# 60s and force-restarts it if it's wedged. Lives outside the agent process
# so it can recover hangs the in-process watchdog can't catch.
$WatchdogTaskName = "ClaudeAgentWatchdog"
Write-Host "==> Registering scheduled task '$WatchdogTaskName' (probes agent every 60s)"

if (Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $WatchdogTaskName -Confirm:$false
}

$wdAction = New-ScheduledTaskAction -Execute $venvPython -Argument "-m agent.watchdog" -WorkingDirectory $InstallDir
$wdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$wdSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew
$wdPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $WatchdogTaskName -Action $wdAction -Trigger $wdTrigger `
    -Settings $wdSettings -Principal $wdPrincipal -Force | Out-Null

Write-Host "==> Starting agent task"
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
Get-ScheduledTask -TaskName $WatchdogTaskName | Select-Object TaskName, State | Format-List

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "  Edit $cfgPath to register your repos, deploys, and allowed services."
Write-Host "  Restart after edits:  schtasks /End /TN $TaskName ; schtasks /Run /TN $TaskName"
Write-Host "  Agent log:            $ConfigDir\logs\agent.log"
Write-Host "  Watchdog log:         $ConfigDir\logs\watchdog.log"
Write-Host "  Watchdog state:       $ConfigDir\watchdog.json"
Write-Host "  Watchdog probes the agent every 60s; force-restarts it on hang."
