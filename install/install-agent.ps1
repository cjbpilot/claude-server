<#
.SYNOPSIS
    Installs the Claude Agent as a Windows service using NSSM.

.DESCRIPTION
    - Stages the repo to $InstallDir (default C:\ClaudeAgent\app).
    - Creates a venv at $VenvDir and installs deps + the agent package.
    - Downloads NSSM if not already on PATH or $NssmPath.
    - Registers the ClaudeAgent service to run 'python -m agent' under NSSM,
      with auto-start, stdout/stderr log capture, and sensible restart rules.
    - Removes any prior pywin32-based ClaudeAgent service first.

.PARAMETER CredsFile
    Path to the Synadia creds file (.creds). Required.

.PARAMETER HostId
    Stable identifier for this machine. Required.

.EXAMPLE
    .\install-agent.ps1 -CredsFile C:\Temp\nats.creds -HostId my-desktop
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CredsFile,
    [Parameter(Mandatory=$true)][string]$HostId,
    [string]$NatsUrl      = "tls://connect.ngs.global",
    [string]$InstallDir   = "C:\ClaudeAgent\app",
    [string]$VenvDir      = "C:\ClaudeAgent\venv",
    [string]$WorkspaceDir = "C:\ClaudeAgent\workspace",
    [string]$ConfigDir    = "C:\ProgramData\ClaudeAgent",
    [string]$PythonExe    = "python",
    [string]$ServiceName  = "ClaudeAgent",
    [string]$NssmPath     = "C:\ClaudeAgent\nssm.exe"
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
Push-Location $InstallDir
try {
    if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
        Write-Warning "install dir is not a git checkout. self_update requires git; see SETUP.md step B6."
    }
} finally {
    Pop-Location
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

# --- Ensure NSSM is available ----------------------------------------------
function Get-NssmExe {
    if (Test-Path $NssmPath) { return $NssmPath }
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$nssmExe = Get-NssmExe
if (-not $nssmExe) {
    Write-Host "==> Downloading NSSM"
    $zipUrl = "https://nssm.cc/release/nssm-2.24.zip"
    $tmpZip = Join-Path $env:TEMP "nssm-2.24.zip"
    $tmpDir = Join-Path $env:TEMP "nssm-2.24"

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $zipUrl -OutFile $tmpZip -UseBasicParsing

    if (Test-Path $tmpDir) { Remove-Item -Recurse -Force $tmpDir }
    Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force

    $arch = if ([Environment]::Is64BitOperatingSystem) { "win64" } else { "win32" }
    $src  = Join-Path $tmpDir "nssm-2.24\$arch\nssm.exe"
    if (-not (Test-Path $src)) { throw "nssm.exe not found in downloaded archive" }

    Ensure-Dir (Split-Path $NssmPath -Parent)
    Copy-Item -Force $src $NssmPath
    $nssmExe = $NssmPath
}
Write-Host "==> Using NSSM at $nssmExe"

# --- Creds file -------------------------------------------------------------
$credsDest = Join-Path $ConfigDir "nats.creds"
Write-Host "==> Installing creds to $credsDest"
if (Test-Path $credsDest) {
    icacls $credsDest /grant "Administrators:F" | Out-Null
    Remove-Item -Force $credsDest
}
Copy-Item -Force $CredsFile $credsDest
icacls $credsDest /inheritance:r /grant "SYSTEM:R" "Administrators:R" | Out-Null

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
    Write-Host "==> Removing existing $ServiceName service"
    Stop-Service $ServiceName -Force -ErrorAction SilentlyContinue
    & $nssmExe remove $ServiceName confirm 2>$null | Out-Null
    if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
        sc.exe delete $ServiceName | Out-Null
    }
    Start-Sleep -Seconds 1
}

# --- Register the service via NSSM -----------------------------------------
Write-Host "==> Registering service '$ServiceName' via NSSM"
$stdoutLog = Join-Path $ConfigDir "logs\stdout.log"
$stderrLog = Join-Path $ConfigDir "logs\stderr.log"

& $nssmExe install $ServiceName $venvPython "-m" "agent" | Out-Null
& $nssmExe set $ServiceName AppDirectory          $InstallDir           | Out-Null
& $nssmExe set $ServiceName DisplayName           "Claude Agent"        | Out-Null
& $nssmExe set $ServiceName Description           "Remote control agent for Claude Code / Cowork." | Out-Null
& $nssmExe set $ServiceName Start                 SERVICE_AUTO_START    | Out-Null
& $nssmExe set $ServiceName AppStdout             $stdoutLog            | Out-Null
& $nssmExe set $ServiceName AppStderr             $stderrLog            | Out-Null
& $nssmExe set $ServiceName AppRotateFiles        1                     | Out-Null
& $nssmExe set $ServiceName AppRotateOnline       1                     | Out-Null
& $nssmExe set $ServiceName AppRotateBytes        10485760              | Out-Null
& $nssmExe set $ServiceName AppStopMethodConsole  5000                  | Out-Null
& $nssmExe set $ServiceName AppStopMethodWindow   5000                  | Out-Null
& $nssmExe set $ServiceName AppStopMethodThreads  5000                  | Out-Null
& $nssmExe set $ServiceName AppExit Default       Restart               | Out-Null
& $nssmExe set $ServiceName AppExit 0             Exit                  | Out-Null
& $nssmExe set $ServiceName AppThrottle           3000                  | Out-Null
& $nssmExe set $ServiceName AppRestartDelay       2000                  | Out-Null

sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/10000 | Out-Null

Write-Host "==> Starting service"
Start-Service -Name $ServiceName
Start-Sleep -Seconds 2
Get-Service -Name $ServiceName | Format-List Name, Status, StartType

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "  Edit $cfgPath to register your repos, deploys, and allowed services."
Write-Host "  Restart after edits:   Restart-Service $ServiceName"
Write-Host "  Agent log:             $ConfigDir\logs\agent.log"
Write-Host "  NSSM stdout/stderr:    $stdoutLog / $stderrLog"
