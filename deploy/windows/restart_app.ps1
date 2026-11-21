# Restart the app after an update: stop uvicorn, run_server.cmd starts it again within 5 s.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*uvicorn server:app*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
foreach ($i in 1..40) {
  Start-Sleep 2
  try { if ((Invoke-WebRequest http://127.0.0.1:8000/ -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) { "app up"; exit 0 } } catch {}
}
"app did not come back - see C:\FarhanFX\server.log"; exit 1
