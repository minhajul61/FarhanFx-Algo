# FarhanFX Algo - VPS Auto Setup Script
# Run this in PowerShell as Administrator on the VPS

Write-Host "=== FarhanFX Algo VPS Setup ===" -ForegroundColor Cyan

# Step 1: Check Python
Write-Host "`n[1/5] Checking Python..." -ForegroundColor Yellow
$pyPath = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $pyPath) {
    Write-Host "ERROR: Python not found! Install from https://python.org first." -ForegroundColor Red
    Write-Host "Make sure to tick 'Add Python to PATH' during install." -ForegroundColor Red
    exit 1
}
$pyVer = python --version
Write-Host "OK: $pyVer" -ForegroundColor Green

# Step 2: Clone or update repo
Write-Host "`n[2/5] Getting latest code from GitHub..." -ForegroundColor Yellow
if (Test-Path "C:\FarhanFX") {
    Set-Location "C:\FarhanFX"
    git pull origin master
    Write-Host "OK: Code updated" -ForegroundColor Green
} else {
    Set-Location "C:\"
    git clone https://github.com/minhajul61/FarhanFx-Algo.git FarhanFX
    Set-Location "C:\FarhanFX"
    Write-Host "OK: Code cloned" -ForegroundColor Green
}

# Step 3: Install Python packages
Write-Host "`n[3/5] Installing Python packages..." -ForegroundColor Yellow
pip install fastapi uvicorn MetaTrader5 pandas numpy --quiet
Write-Host "OK: Packages installed" -ForegroundColor Green

# Step 4: Open Firewall port 8000
Write-Host "`n[4/5] Opening Firewall port 8000..." -ForegroundColor Yellow
netsh advfirewall firewall add rule name="FarhanFX-8000" dir=in action=allow protocol=TCP localport=8000 | Out-Null
Write-Host "OK: Port 8000 opened" -ForegroundColor Green

# Step 5: Create desktop shortcut to start server
Write-Host "`n[5/5] Creating Start Server shortcut on Desktop..." -ForegroundColor Yellow
$bat = @"
@echo off
title FarhanFX Algo Server
cd /d C:\FarhanFX
echo Starting FarhanFX Algo Server...
echo Website: http://103.127.146.125:8000
echo.
python server.py
pause
"@
$bat | Out-File -FilePath "C:\FarhanFX\START_SERVER.bat" -Encoding ascii

$WshShell = New-Object -ComObject WScript.Shell
$shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\FarhanFX Server.lnk")
$shortcut.TargetPath = "C:\FarhanFX\START_SERVER.bat"
$shortcut.WorkingDirectory = "C:\FarhanFX"
$shortcut.IconLocation = "C:\Windows\System32\cmd.exe"
$shortcut.Save()
Write-Host "OK: Shortcut created on Desktop" -ForegroundColor Green

Write-Host "`n=== SETUP COMPLETE ===" -ForegroundColor Cyan
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Install MetaTrader 5 from your CXM Direct broker" -ForegroundColor White
Write-Host "  2. Login to MT5 with your account" -ForegroundColor White
Write-Host "  3. Double-click 'FarhanFX Server' on Desktop to start" -ForegroundColor White
Write-Host "  4. Open browser: http://103.127.146.125:8000" -ForegroundColor White
Write-Host ""
