<#
.SYNOPSIS
    Removes the Claude Agent scheduled task (and any legacy NSSM service).

.DESCRIPTION
    Stops and removes the task. Leaves code, venv, config, and logs in
    place by default. Pass -Purge to delete C:\ClaudeAgent and
    C:\ProgramData\ClaudeAgent too.
#>
[CmdletBinding()]
param(
    [string]$TaskName    = "ClaudeAgent",
    [string]$ServiceName = "ClaudeAgent",
    [string]$ConfigDir   = "C:\ProgramData\ClaudeAgent",
    [switch]$Purge
)

$ErrorActionPreference = "Stop"

$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$p  = New-Object System.Security.Principal.WindowsPrincipal($id)
if (-not $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run from an elevated PowerShell."
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "==> Stopping task $TaskName"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "==> Task removed"
} else {
    Write-Host "task $TaskName not registered"
}

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "==> Removing legacy $ServiceName service"
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    $legacyNssm = "C:\ClaudeAgent\nssm.exe"
    if (Test-Path $legacyNssm) {
        & $legacyNssm remove $ServiceName confirm | Out-Null
    }
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        sc.exe delete $ServiceName | Out-Null
    }
}

if ($Purge) {
    Write-Host "==> Purging C:\ClaudeAgent and $ConfigDir"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "C:\ClaudeAgent"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ConfigDir
}

Write-Host "Uninstall complete."
