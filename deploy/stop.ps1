# Stop the services started by deploy\start.ps1.
$Logs = Join-Path (Split-Path -Parent $PSScriptRoot) "data\logs"
foreach ($name in @("caddy", "bot", "scheduler", "api")) {
    $pidFile = Join-Path $Logs "$name.pid"
    if (Test-Path $pidFile) {
        $procId = [int](Get-Content $pidFile)
        try { Stop-Process -Id $procId -Confirm:$false -ErrorAction Stop; Write-Host "stopped $name ($procId)" }
        catch { Write-Host "$name ($procId) was not running" }
        Remove-Item $pidFile -Confirm:$false
    }
}
