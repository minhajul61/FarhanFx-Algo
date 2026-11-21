[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"
New-Item -ItemType Directory -Force C:\FarhanFX\caddy | Out-Null
Set-Location C:\FarhanFX\caddy
if (-not (Test-Path caddy.exe)) {
  Invoke-WebRequest "https://github.com/caddyserver/caddy/releases/download/v2.8.4/caddy_2.8.4_windows_amd64.zip" -OutFile caddy.zip -UseBasicParsing
  Expand-Archive caddy.zip -DestinationPath . -Force
  Remove-Item caddy.zip
}
Move-Item -Force C:\FarhanFX\Caddyfile C:\FarhanFX\caddy\Caddyfile
Move-Item -Force C:\FarhanFX\run_caddy.cmd C:\FarhanFX\caddy\run_caddy.cmd
.\caddy.exe validate --config Caddyfile --adapter caddyfile 2>&1 | Select -Last 1
$a = New-ScheduledTaskAction -Execute "C:\FarhanFX\caddy\run_caddy.cmd" -WorkingDirectory "C:\FarhanFX\caddy"
$t = New-ScheduledTaskTrigger -AtStartup
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$p = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName "FarhanFX-Caddy" -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
Start-ScheduledTask -TaskName "FarhanFX-Caddy"
Start-Sleep 10
(Get-ScheduledTask FarhanFX-Caddy).State
Get-NetTCPConnection -State Listen -LocalPort 80,443 -EA SilentlyContinue | Select LocalAddress,LocalPort | Format-Table -AutoSize
Get-Content caddy.log -Tail 6
