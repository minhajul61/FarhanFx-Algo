Remove-Item C:\FarhanFX\users.json -Force -EA SilentlyContinue
$a = New-ScheduledTaskAction -Execute "C:\FarhanFX\run_server.cmd" -WorkingDirectory "C:\FarhanFX"
$t = New-ScheduledTaskTrigger -AtStartup
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$p = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "FarhanFX" -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
Start-ScheduledTask -TaskName "FarhanFX"
Start-Sleep 25
(Get-ScheduledTask FarhanFX).State
try { (Invoke-WebRequest http://127.0.0.1:8000/admin -UseBasicParsing -TimeoutSec 15).StatusCode } catch { "http error: $_" }
Get-Content C:\FarhanFX\server.log -Tail 8
