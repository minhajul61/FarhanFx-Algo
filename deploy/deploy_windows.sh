#!/usr/bin/env bash
# Update FarhanFX on the Windows VPS from this Mac (SSH key: ~/.ssh/farhanfx_vps).
#   deploy/deploy_windows.sh
# Runtime data on the VPS (users, bots, ledger, keys) is never overwritten.
# First-time setup (Python, the FarhanFX + FarhanFX-Caddy boot tasks) is in deploy/windows/.
set -euo pipefail
HOST="${FARHANFX_HOST:-Administrator@212.24.127.115}"
KEY="${FARHANFX_KEY:-$HOME/.ssh/farhanfx_vps}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP_FILES=(server.py index.html brain.py engine.py risk_manager.py autopilot.py market_guard.py
           custom_strategy.py extra_strategies.py trade_ledger.py requirements.txt set_admin_password.py)
cd "$SRC"
echo "== checks"
python3 -m py_compile server.py brain.py engine.py risk_manager.py autopilot.py market_guard.py custom_strategy.py extra_strategies.py trade_ledger.py set_admin_password.py
if [ -x .venv/bin/python ]; then .venv/bin/python -W ignore tests/test_live_engine.py | tail -1; fi
echo "== upload"
scp -i "$KEY" -q "${APP_FILES[@]}" deploy/windows/restart_app.ps1 "$HOST:C:/FarhanFX/"
echo "== restart"
ssh -i "$KEY" -o ConnectTimeout=15 "$HOST" 'powershell -ExecutionPolicy Bypass -File C:\FarhanFX\restart_app.ps1'
