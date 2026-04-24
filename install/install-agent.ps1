<#
.SYNOPSIS
    Installs the Claude Agent as a Windows service.

.DESCRIPTION
    - Expects this script to live inside a clone of the claude-server repo.
    - Clones/moves the repo to $InstallDir (default C:\ClaudeAgent\app).
    - Creates a venv at C:\ClaudeAgent\venv.
    - Installs Python requirements.
    - Seeds C:\ProgramData\ClaudeAgent with agent.toml (if absent) and the
      Synadia creds file you pass in.
    - Registers the 'ClaudeAgent' Windows service with auto-start and
      automatic restart on failure.

.PARAMETER CredsFile
    Path to the Synadia Cloud creds file (.creds). Required.

.PARAMETER HostId
    Stable identifier for this machine. Required.

.PARAMETER NatsUrl
    NATS URL. Default tls://connect.ngs.global (Synadia NGS).

.PARAMETER InstallDir
    Where the agent code lives. Default C:\ClaudeAgent\app.

.PARAMETER PythonExe
    Python interpreter used to create the venv. Default 'python'.

.EXAMPLE
    .\install-agent.ps1 -CredsFile C:\Users\me\Downloads\nats.creds -HostId my-desktop
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$CredsFile,
    [Parameter(Mandatory=$true)][string]$HostId,
    [string]$NatsUrl = "tls://connect.ngs.global",
    [string]$InstallDir = "C:\ClaudeAgent\app",
    [string]$VenvDir = "C:\ClaudeAgent\venv",
    [string]$WorkspaceDir = "C:\ClaudeAgent\workspace",
    [string]$ConfigDir = "C:\ProgramData\ClaudeAgent",
    [string]$PythonExe = "python",
    [string]$ServiceName = "ClaudeAgent"
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

Write-Host "==> Preparing directories"
Ensure-Dir (Split-Path $InstallDir -Parent)
Ensure-Dir $WorkspaceDir
Ensure-Dir $ConfigDir
Ensure-Dir (Join-Path $ConfigDir "logs")

# --- Stage code -------------------------------------------------------------
# This script lives in install/ inside the repo. Copy the repo to $InstallDir.
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Write-Host "==> Staging code from $repoRoot to $InstallDir"
if (Test-Path $InstallDir) {
    # Preserve .git if the user set it up already; otherwise just overwrite.
    robocopy "$repoRoot" "$InstallDir" /MIR /XD ".venv" "__pycache__" /NFL /NDL /NJH /NJS | Out-Null
} else {
    Ensure-Dir $InstallDir
    robocopy "$repoRoot" "$InstallDir" /E /XD ".venv" "__pycache__" /NFL /NDL /NJH /NJS | Out-Null
}
if (-not (Test-Path (Join-Path $InstallDir "agent\service.py"))) {
    throw "Staging failed - agent\service.py missing in $InstallDir."
}

# --- Ensure the install dir is a git checkout so self_update works ----------
Push-Location $InstallDir
try {
    if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
        Write-Warning "install dir is not a git checkout. self_update requires git."
        Write-Warning "Run 'git init && git remote add origin <url> && git fetch && git reset --hard origin/main' in $InstallDir, or re-stage from a git clone."
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

# --- Creds file -------------------------------------------------------------
$credsDest = Join-Path $ConfigDir "nats.creds"
Write-Host "==> Installing creds to $credsDest"
Copy-Item -Force $CredsFile $credsDest
# Lock ACL to SYSTEM + Administrators.
icacls $credsDest /inheritance:r /grant "SYSTEM:R" "Administrators:R" | Out-Null

# --- agent.toml -------------------------------------------------------------
$cfgPath = Join-Path $ConfigDir "agent.toml"
if (-not (Test-Path $cfgPath)) {
    Write-Host "==> Seeding $cfgPath from agent.toml.example"
    $example = Get-Content (Join-Path $InstallDir "agent\agent.toml.example") -Raw
    $example = $example `
        -replace 'host_id = "my-desktop"',            ("host_id = `"" + $HostId + "`"") `
        -replace 'C:/ClaudeAgent/workspace',           ($WorkspaceDir.Replace('\','/')) `
        -replace 'C:/ClaudeAgent/app',                 ($InstallDir.Replace('\','/')) `
        -replace 'C:/ClaudeAgent/venv/Scripts/python.exe', ($venvPython.Replace('\','/')) `
        -replace 'C:/ProgramData/ClaudeAgent/nats.creds',   ($credsDest.Replace('\','/')) `
        -replace 'tls://connect.ngs.global',           $NatsUrl
    Set-Content -Path $cfgPath -Value $example -Encoding UTF8
} else {
    Write-Host "==> Keeping existing $cfgPath"
}

# --- Install / update the Windows service -----------------------------------
Write-Host "==> Registering Windows service '$ServiceName'"

# pywin32 service registration must be run with the agent importable on sys.path.
$env:PYTHONPATH = $InstallDir
Push-Location $InstallDir
try {
    # Remove any old copy so we can re-register cleanly with the new --startup arg.
    & $venvPython -m agent.service stop 2>$null
    & $venvPython -m agent.service remove 2>$null

    & $venvPython -m agent.service --startup=auto install
    if ($LASTEXITCODE -ne 0) { throw "service install failed" }
} finally {
    Pop-Location
}

# Configure recovery: restart after 5s on any failure.
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/10000 | Out-Null
# Give it a friendly description.
sc.exe description $ServiceName "Claude Agent - remote control for Claude Code / Cowork." | Out-Null

Write-Host "==> Starting service"
Start-Service -Name $ServiceName
Get-Service -Name $ServiceName | Format-List Name, Status, StartType

Write-Host ""
Write-Host "Install complete. Next steps:" -ForegroundColor Green
Write-Host "  - Edit $cfgPath to register your repos, deploys, and allowed services."
Write-Host "  - Restart the service after config changes:  Restart-Service $ServiceName"
Write-Host "  - Logs: $ConfigDir\logs\agent.log"
