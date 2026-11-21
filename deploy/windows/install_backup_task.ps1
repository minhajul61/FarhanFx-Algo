# Registers the daily data backup (03:30 every day, as SYSTEM) and runs it once now.
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\FarhanFX\backup_data.ps1"
$t = New-ScheduledTaskTrigger -Daily -At 3:30am
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$p = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "FarhanFX-Backup" -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
powershell -NoProfile -ExecutionPolicy Bypass -File C:\FarhanFX\backup_data.ps1
(Get-ChildItem C:\FarhanFX\backups -Filter *.zip | Sort-Object LastWriteTime | Select-Object -Last 1 | ForEach-Object { "$($_.Name) $([math]::Round($_.Length/1KB)) KB" })
