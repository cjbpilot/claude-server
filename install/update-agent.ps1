<#
.SYNOPSIS
    Manual update helper — does the same thing the self_update MCP tool does.

.DESCRIPTION
    Useful when self_update can't run (service stopped, NATS unreachable, etc).
    Stops the service, git pulls, reinstalls deps, starts the service.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = "ClaudeAgent",
    [string]$InstallDir  = "C:\ClaudeAgent\app",
    [string]$VenvDir     = "C:\ClaudeAgent\venv"
)

$ErrorActionPreference = "Stop"
$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$p  = New-Object System.Security.Principal.WindowsPrincipal($id)
if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run from an elevated PowerShell."
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "==> Stopping $ServiceName"
Stop-Service -Name $ServiceName -ErrorAction SilentlyContinue

Push-Location $InstallDir
try {
    Write-Host "==> git pull"
    git fetch --all --prune
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull failed" }

    Write-Host "==> pip install"
    & $venvPython -m pip install -r (Join-Path $InstallDir "agent\requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
} finally {
    Pop-Location
}

Write-Host "==> Starting $ServiceName"
Start-Service -Name $ServiceName
Get-Service $ServiceName | Format-List Name, Status
