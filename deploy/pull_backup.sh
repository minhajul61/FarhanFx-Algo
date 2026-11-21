#!/usr/bin/env bash
# Copy the VPS's newest data backups to this Mac (a second copy off the server).
#   deploy/pull_backup.sh
set -euo pipefail
HOST="${FARHANFX_HOST:-Administrator@212.24.127.115}"
KEY="${FARHANFX_KEY:-$HOME/.ssh/farhanfx_vps}"
DEST="${FARHANFX_BACKUP_DIR:-$HOME/FarhanFX-backups}"
mkdir -p "$DEST"
for f in $(ssh -i "$KEY" -o ConnectTimeout=15 "$HOST" 'Get-ChildItem C:\FarhanFX\backups -Filter *.zip | Sort-Object LastWriteTime -Descending | Select-Object -First 7 | ForEach-Object Name' | tr -d '\r'); do
  [ -f "$DEST/$f" ] || scp -i "$KEY" -q "$HOST:C:/FarhanFX/backups/$f" "$DEST/" && echo "pulled $f"
done
ls -1t "$DEST" | head -7
