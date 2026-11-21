@echo off
rem HTTPS front door for farhanfxalgo.in: started at boot by the "FarhanFX-Caddy" scheduled task.
cd /d C:\FarhanFX\caddy
set XDG_DATA_HOME=C:\FarhanFX\caddy\data
set XDG_CONFIG_HOME=C:\FarhanFX\caddy\config
:loop
caddy.exe run --config Caddyfile --adapter caddyfile >> caddy.log 2>&1
timeout /t 5 /nobreak >nul
goto loop
