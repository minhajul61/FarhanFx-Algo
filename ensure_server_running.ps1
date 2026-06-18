# FarhanFX Algo - local watchdog
# Ensures server.py is running on this machine. Safe to run repeatedly:
# it only starts a new process when neither a healthy HTTP response nor
# an existing server.py process is found, so it never double-launches.

$projectDir = "e:\Farhan Fx Algo"
$logFile    = Join-Path $projectDir "watchdog_local.log"
$pythonExe  = "C:\Users\BeingPe\AppData\Local\Python\bin\python.exe"

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $logFile -Value $line
}

if (Test-Path $logFile) {
    $lines = Get-Content $logFile
    if ($lines.Count -gt 1000) {
        $lines[-1000..-1] | Set-Content $logFile
    }
}

$healthy = $false
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/strategy/list" -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
    if ($resp.StatusCode -eq 200) { $healthy = $true }
} catch {
    $healthy = $false
}

if ($healthy) {
    exit 0
}

$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -like "*server.py*" }

if ($existing) {
    Write-Log "Not healthy yet but server.py process already running (PID $($existing.ProcessId)) - waiting, not restarting"
    exit 0
}

Write-Log "Server down (no process, no healthy response) - starting server.py"
Start-Process -FilePath $pythonExe -ArgumentList "server.py" -WorkingDirectory $projectDir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $projectDir "server_out.log") `
    -RedirectStandardError  (Join-Path $projectDir "server_err.log")
Write-Log "Start-Process issued for server.py"
