# Daily backup of FarhanFX runtime data (accounts, bots, trades, wallets, settings).
# Run by the "FarhanFX-Backup" task every day at 03:30; keeps the last 30 days.
$ErrorActionPreference = "Stop"
$src  = "C:\FarhanFX"
$root = "C:\FarhanFX\backups"
New-Item -ItemType Directory -Force $root | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$tmp = Join-Path $env:TEMP "farhanfx_$stamp"
New-Item -ItemType Directory -Force $tmp | Out-Null
# copy first, then zip the copy: the app keeps writing while it runs
Get-ChildItem $src -File -Filter *.json | Where-Object { $_.Name -ne "auth_sessions.json" } | Copy-Item -Destination $tmp   # login sessions are not worth keeping
if (Test-Path "$src\payment_proofs") { Copy-Item "$src\payment_proofs" -Destination $tmp -Recurse }
Compress-Archive -Path "$tmp\*" -DestinationPath (Join-Path $root "farhanfx_$stamp.zip") -Force
Remove-Item $tmp -Recurse -Force
Get-ChildItem $root -Filter "farhanfx_*.zip" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
"backup ok: farhanfx_$stamp.zip"
