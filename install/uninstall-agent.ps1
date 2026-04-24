<#
.SYNOPSIS
    Removes the Claude Agent Windows service (NSSM-managed).

.DESCRIPTION
    Stops and removes the service. Leaves code, venv, config, and logs in
    place by default. Pass -Purge to delete C:\ClaudeAgent and
    C:\ProgramData\ClaudeAgent too.
#>
[CmdletBinding()]
param(
    [string]$ServiceName = "ClaudeAgent",
    [string]$ConfigDir   = "C:\ProgramData\ClaudeAgent",
    [string]$NssmPath    = "C:\ClaudeAgent\nssm.exe",
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$p  = New-Object System.Security.Principal.WindowsPrincipal($id)
if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run from an elevated PowerShell."
}

function Resolve-Nssm {
    if (Test-Path $NssmPath) { return $NssmPath }
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$nssmExe = Resolve-Nssm

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "==> Stopping $ServiceName"
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue

    if ($nssmExe) {
        & $nssmExe remove $ServiceName confirm | Out-Null
    }
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        sc.exe delete $ServiceName | Out-Null
    }
    Write-Host "==> Removed"
} else {
    Write-Host "service $ServiceName not installed"
}

if ($Purge) {
    Write-Host "==> Purging C:\ClaudeAgent and $ConfigDir"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "C:\ClaudeAgent"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ConfigDir
}

Write-Host "Uninstall complete."
