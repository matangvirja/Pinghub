<#
.SYNOPSIS
    Start PingHub and print the address other devices can use.

.DESCRIPTION
    Starts the Docker Compose stack, waits until the web UI is reachable,
    detects this computer's Wi-Fi/LAN IPv4 address, and prints both URLs:
      * http://localhost:3000        (this computer only)
      * http://<lan-ip>:3000         (phones and other laptops on the same Wi-Fi)

    The LAN address is saved to .env as PINGHUB_LAN_IP so the in-app
    "Use on another device" panel can show the exact URL.

.PARAMETER NoBuild
    Start without rebuilding images (faster after the first run).

.PARAMETER NoBrowser
    Do not open the default browser automatically.

.PARAMETER OpenFirewall
    Add a Windows Firewall inbound rule for the UI port. Requires Administrator.

.EXAMPLE
    .\scripts\start.ps1

.EXAMPLE
    .\scripts\start.ps1 -NoBuild -NoBrowser
#>

[CmdletBinding()]
param(
    [switch]$NoBuild,
    [switch]$NoBrowser,
    [switch]$OpenFirewall
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$uiPort = 3000
if ($env:PINGHUB_UI_PORT) {
    $parsed = 0
    if ([int]::TryParse($env:PINGHUB_UI_PORT, [ref]$parsed) -and $parsed -gt 0) {
        $uiPort = $parsed
    }
}

function Write-Step($message) {
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Write-Ok($message) {
    Write-Host "  OK  $message" -ForegroundColor Green
}

function Write-Warn($message) {
    Write-Host "  !!  $message" -ForegroundColor Yellow
}

function Get-PingHubLanIp {
    try {
        $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
            Sort-Object RouteMetric |
            Select-Object -First 1
        if ($route) {
            $address = Get-NetIPAddress -InterfaceIndex $route.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
                Select-Object -First 1
            if ($address) { return $address.IPAddress }
        }
    } catch { }

    try {
        $fallback = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.InterfaceAlias -notmatch "vEthernet|WSL|Loopback|Hyper-V"
            } |
            Select-Object -First 1
        if ($fallback) { return $fallback.IPAddress }
    } catch { }

    return $null
}

function Set-EnvValue($path, $key, $value) {
    $lines = @()
    if (Test-Path $path) { $lines = @(Get-Content $path) }

    $found = $false
    $output = foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($key))\s*=") {
            if (-not $found) {
                "$key=$value"
                $found = $true
            }
        } else {
            $line
        }
    }

    if (-not $found) {
        $output = @($output) + "$key=$value"
    }

    Set-Content -Path $path -Value $output -Encoding ASCII
}

# --- Preconditions ---------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker was not found." -ForegroundColor Red
    Write-Host "Install Docker Desktop, start it, then run this script again."
    exit 1
}

try {
    docker info *> $null
} catch {
    Write-Host "Docker Desktop is not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait for the whale icon, then run this script again."
    exit 1
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker Desktop is not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait for the whale icon, then run this script again."
    exit 1
}

# --- LAN address ------------------------------------------------------------
$lanIp = Get-PingHubLanIp
$envPath = Join-Path $root ".env"
Set-EnvValue $envPath "PINGHUB_UI_PORT" "$uiPort"
if ($lanIp) {
    Set-EnvValue $envPath "PINGHUB_LAN_IP" "$lanIp"
    Write-Step "Detected this computer's LAN address: $lanIp"
} else {
    Write-Warn "Could not detect a LAN IPv4 address. Local access will still work."
}

# --- Start the stack --------------------------------------------------------
if ($NoBuild) {
    Write-Step "Starting PingHub containers..."
    docker compose up -d
} else {
    Write-Step "Building and starting PingHub (the first build can take a few minutes)..."
    docker compose up -d --build
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "docker compose failed. Scroll up for the reason." -ForegroundColor Red
    exit 1
}

# --- Wait until the UI (and therefore the gateway + message-api) is ready ---
$localUrl = "http://localhost:$uiPort"
# Probe 127.0.0.1: on some Windows machines resolving "localhost" stalls for a
# couple of seconds (IPv6 first), which is longer than a short HTTP timeout.
$probeUrl = "http://127.0.0.1:$uiPort"
Write-Step "Waiting for the web app at $localUrl ..."
$ready = $false
for ($attempt = 1; $attempt -le 120; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $probeUrl -TimeoutSec 10
        if ($response.StatusCode -eq 200) {
            # The static page is up as soon as nginx starts; also require the
            # gateway to report every backend node healthy before we declare
            # success. Otherwise "message-api unavailable" can greet the user.
            $health = Invoke-WebRequest -UseBasicParsing -Uri "$probeUrl/api/health" -TimeoutSec 10
            if ($health.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
    } catch { }
    Start-Sleep -Seconds 2
}

if (-not $ready) {
    Write-Host ""
    Write-Host "The web app did not become ready in time." -ForegroundColor Red
    Write-Host "Run:  docker compose ps"
    Write-Host "Then: docker compose logs gateway message-api ui"
    exit 1
}

# --- Optional firewall rule -------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($OpenFirewall) {
    if (-not $isAdmin) {
        Write-Warn "OpenFirewall needs Administrator. Right-click PowerShell -> Run as administrator, then retry."
    } else {
        $ruleName = "PingHub UI (TCP $uiPort)"
        if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $uiPort -Profile Private | Out-Null
            Write-Ok "Firewall rule added for TCP $uiPort (Private networks)."
        } else {
            Write-Ok "Firewall rule already exists for TCP $uiPort."
        }
    }
}

# --- Done -------------------------------------------------------------------
Write-Host ""
Write-Host "=============================================================" -ForegroundColor Green
Write-Host "  PingHub is ready" -ForegroundColor Green
Write-Host "=============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  On this computer:     $localUrl"
if ($lanIp) {
    Write-Host "  On other devices:     http://${lanIp}:$uiPort" -ForegroundColor Cyan
} else {
    Write-Host "  On other devices:     http://<this-computer-ip>:$uiPort"
}
Write-Host ""
Write-Host "  How to chat with someone on another device:"
Write-Host "    1. Both devices join the same Wi-Fi."
Write-Host "    2. Open the 'other devices' address in any browser."
Write-Host "    3. Each person creates a profile (no password)."
Write-Host "    4. Click 'New chat', paste the other person's 8-character ID, send."
Write-Host ""
Write-Host "  The server-side pieces (gateway, queue, workers, database) run here."
Write-Host "  Phones only need a browser; nothing is installed on them."
Write-Host ""

if ($lanIp -and -not $OpenFirewall) {
    Write-Host "  If another device cannot connect, allow the port once as Administrator:" -ForegroundColor Yellow
    Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1"
    Write-Host ""
}

if (-not $NoBrowser) {
    Start-Process $localUrl
}
