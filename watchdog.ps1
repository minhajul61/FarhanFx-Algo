$ProgressPreference = "SilentlyContinue"
$logFile = "C:\FarhanFX\watchdog.log"
$maxLogLines = 500

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $logFile -Value $line
}

function Clear-LogFile {
    if (Test-Path $logFile) {
        $lines = Get-Content $logFile
        if ($lines.Count -gt $maxLogLines) {
            $lines[-$maxLogLines..-1] | Set-Content $logFile
        }
    }
}

$healthy = $false
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/account" -TimeoutSec 10 -UseBasicParsing -ErrorAction Stop
    if ($resp.StatusCode -eq 200) {
        $healthy = $true
    } else {
        Write-Log "UNHEALTHY - HTTP $($resp.StatusCode)"
    }
} catch {
    Write-Log "UNHEALTHY - request failed: $($_.Exception.Message)"
}

$procRunning = Get-Process python -ErrorAction SilentlyContinue
if (-not $procRunning) {
    Write-Log "UNHEALTHY - no python process running"
    $healthy = $false
}

if (-not $healthy) {
    Write-Log "RESTARTING server via Task Scheduler"
    Stop-ScheduledTask -TaskName FarhanFX -ErrorAction SilentlyContinue
    Stop-Process -Name python -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName FarhanFX
    Write-Log "RESTART triggered"
}

Clear-LogFile
