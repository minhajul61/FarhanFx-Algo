@echo off
rem FarhanFX app server: started at boot by the "FarhanFX" scheduled task, restarts itself if it stops.
cd /d C:\FarhanFX
set FARHANFX_PRODUCTION=1
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
:loop
.venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 >> server.log 2>&1
timeout /t 5 /nobreak >nul
goto loop
