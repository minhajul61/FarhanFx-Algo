@echo off
title FarhanFX Algo Server
color 0A
echo.
echo  ========================================
echo   FARHANFX ALGO SERVER
echo  ========================================
echo.
echo  Make sure MetaTrader 5 is OPEN and LOGGED IN
echo  before this server can connect to MT5.
echo.
echo  Server starting on http://127.0.0.1:8000
echo  Press CTRL+C to stop
echo.
cd /d "e:\Farhan Fx Algo"
python server.py
pause
