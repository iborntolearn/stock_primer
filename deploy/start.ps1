# Start stock_primer on this PC: API, scheduler, Telegram bot (if configured) and Caddy.
#
#   powershell -ExecutionPolicy Bypass -File deploy\start.ps1 [-Site stock-primer.tailnet.ts.net] [-NoBuild]
#
# Each service runs hidden with its output in data\logs. Stop everything with deploy\stop.ps1.
# To start at logon: Task Scheduler -> Create Task -> "At log on" -> this command (see deploy\README.md).
param(
    [string]$Site = "localhost",
    [switch]$NoBuild
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Logs = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force $Logs | Out-Null

if (-not (Get-Command caddy -ErrorAction SilentlyContinue)) {
    throw "Caddy is not installed. Install it with:  winget install CaddyServer.Caddy   (then open a new terminal)"
}
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile) -or -not (Select-String -Path $envFile -Pattern "^STOCK_PRIMER_AUTH_PASSWORD_HASH=." -Quiet)) {
    throw "Login is not set up. Run:  .venv\Scripts\python -m api.auth setup"
}

$Web = Join-Path $Root "web\dist\web\browser"
if (-not $NoBuild) {
    Push-Location (Join-Path $Root "web")
    npx ng build --configuration production
    Pop-Location
}
if (-not (Test-Path (Join-Path $Web "index.html"))) { throw "No web build at $Web" }

function Start-Service-Proc($name, $file, $argList) {
    $p = Start-Process -FilePath $file -ArgumentList $argList -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Logs "$name.out.log") -RedirectStandardError (Join-Path $Logs "$name.err.log")
    Set-Content -Path (Join-Path $Logs "$name.pid") -Value $p.Id
    Write-Host "started $name (pid $($p.Id))"
}

Start-Service-Proc "api" $Py @("-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000", "--proxy-headers")
Start-Service-Proc "scheduler" $Py @("-m", "scheduler.main")
if (Select-String -Path $envFile -Pattern "^STOCK_PRIMER_TELEGRAM_BOT_TOKEN=." -Quiet) {
    Start-Service-Proc "bot" $Py @("-m", "bot.main")
}

$env:STOCK_PRIMER_SITE = $Site
$env:STOCK_PRIMER_WEB_ROOT = $Web
$env:STOCK_PRIMER_LOG_DIR = $Logs
Start-Service-Proc "caddy" "caddy" @("run", "--config", (Join-Path $PSScriptRoot "Caddyfile"), "--adapter", "caddyfile")
Write-Host "Open https://$Site"
