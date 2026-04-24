<#
.SYNOPSIS
    Removes the Claude Agent Windows service.

.DESCRIPTION
    Stops and removes the service. Leaves code, venv, config, and logs in
    place by default so you can reinstall without losing state. Pass
    -Purge to delete C:\ClaudeAgent and C:\ProgramData\ClaudeAgent too.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = "ClaudeAgent",
    [string]$InstallDir  = "C:\ClaudeAgent\app",
    [string]$VenvDir     = "C:\ClaudeAgent\venv",
    [string]$ConfigDir   = "C:\ProgramData\ClaudeAgent",
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$p  = New-Object System.Security.Principal.WindowsPrincipal($id)
if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run from an elevated PowerShell."
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "==> Stopping $ServiceName"
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue

    $bootstrap = Join-Path $InstallDir "install_service.py"
    if ((Test-Path $venvPython) -and (Test-Path $bootstrap)) {
        Push-Location $InstallDir
        try {
            & $venvPython $bootstrap remove
        } finally {
            Pop-Location
        }
    } else {
        sc.exe delete $ServiceName | Out-Null
    }
} else {
    Write-Host "service $ServiceName not installed"
}

if ($Purge) {
    Write-Host "==> Purging C:\ClaudeAgent and $ConfigDir"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "C:\ClaudeAgent"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ConfigDir
}

Write-Host "Uninstall complete."
