<#
.SYNOPSIS
    Allow other devices on your Wi-Fi to reach the PingHub web app.

.DESCRIPTION
    Adds an inbound Windows Firewall rule for the PingHub UI port (default 3000)
    on Private networks. Run this once, as Administrator, if a phone or another
    laptop cannot open http://<this-computer-ip>:3000.

.EXAMPLE
    Right-click PowerShell -> "Run as administrator", then:
    .\scripts\allow-lan.ps1
#>

[CmdletBinding()]
param(
    [int]$Port = 3000
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "This script needs Administrator rights." -ForegroundColor Red
    Write-Host "Right-click PowerShell, choose 'Run as administrator', then run:"
    Write-Host "  .\scripts\allow-lan.ps1 -Port $Port"
    exit 1
}

$ruleName = "PingHub UI (TCP $Port)"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue

if ($existing) {
    Enable-NetFirewallRule -DisplayName $ruleName | Out-Null
    Write-Host "Firewall rule already exists and is enabled: $ruleName" -ForegroundColor Green
} else {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
    Write-Host "Added inbound firewall rule: $ruleName (Private networks)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Make sure the other device is on the same Wi-Fi, then open:"
Write-Host "  http://<this-computer-ip>:$Port" -ForegroundColor Cyan
