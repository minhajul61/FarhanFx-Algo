#!/usr/bin/env bash
# Deploy FarhanFX from this Mac to the Hostinger VPS.
#   First time:  FARHANFX_HOST=root@<vps-ip> deploy/deploy.sh --setup
#   Updates:     FARHANFX_HOST=root@<vps-ip> deploy/deploy.sh
# Runtime data on the server (users, bots, ledger, keys) is never overwritten.
set -euo pipefail
HOST="${FARHANFX_HOST:?set FARHANFX_HOST=root@<vps-ip>}"
KEY="${FARHANFX_KEY:-$HOME/.ssh/farhanfx_vps}"
DOMAIN="${FARHANFX_DOMAIN:-farhanfxalgo.in}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
APP_FILES=(server.py index.html brain.py engine.py risk_manager.py autopilot.py market_guard.py
           custom_strategy.py trade_ledger.py requirements.txt set_admin_password.py)

cd "$SRC"
echo "== checks"
python3 -m py_compile server.py brain.py engine.py risk_manager.py autopilot.py market_guard.py custom_strategy.py trade_ledger.py set_admin_password.py
if [ -x .venv/bin/python ]; then .venv/bin/python -W ignore tests/test_live_engine.py | tail -1; fi

if [ "${1:-}" = "--setup" ]; then
  echo "== server setup"
  rsync -az -e "${SSH[*]}" deploy/setup_vps.sh deploy/farhanfx.service deploy/Caddyfile "$HOST:/root/farhanfx-setup/"
  "${SSH[@]}" "$HOST" "bash /root/farhanfx-setup/setup_vps.sh $DOMAIN"
fi

echo "== upload app"
rsync -az --chmod=F640 -e "${SSH[*]}" "${APP_FILES[@]}" "$HOST:/opt/farhanfx/"
"${SSH[@]}" "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/farhanfx
chown -R farhanfx:farhanfx /opt/farhanfx
sudo -u farhanfx bash -c 'test -d .venv || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt'
systemctl restart farhanfx
for i in $(seq 1 30); do
  curl -fsS -o /dev/null http://127.0.0.1:8000/ && break
  sleep 1
done
systemctl is-active farhanfx
curl -fsS -o /dev/null -w "app answers locally: HTTP %{http_code}\n" http://127.0.0.1:8000/
REMOTE
echo "== done → https://$DOMAIN"
