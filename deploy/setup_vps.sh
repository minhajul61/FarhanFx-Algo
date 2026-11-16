#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 VPS for FarhanFX (run as root).
# Safe to run again. Usage: bash setup_vps.sh farhanfxalgo.in
set -euo pipefail
DOMAIN="${1:?usage: setup_vps.sh <domain>}"
APP=/opt/farhanfx
HERE="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential git ufw curl rsync \
                   debian-keyring debian-archive-keyring apt-transport-https gnupg unattended-upgrades

echo "== caddy (HTTPS reverse proxy)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "== app user and folder"
id farhanfx >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin farhanfx
mkdir -p "$APP"
chown -R farhanfx:farhanfx "$APP"
chmod 750 "$APP"

echo "== swap (pip builds need memory on small plans)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== firewall: only SSH, HTTP, HTTPS"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "== SSH: keys only"
if grep -q "^ssh-" /root/.ssh/authorized_keys 2>/dev/null; then
  mkdir -p /etc/ssh/sshd_config.d
  printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\n' > /etc/ssh/sshd_config.d/90-farhanfx.conf
  systemctl reload ssh || systemctl reload sshd || true
fi

echo "== clock in UTC, automatic security updates"
timedatectl set-timezone UTC || true
dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo "== services"
install -m 644 "$HERE/farhanfx.service" /etc/systemd/system/farhanfx.service
sed "s/__DOMAIN__/$DOMAIN/g" "$HERE/Caddyfile" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl enable farhanfx caddy
systemctl restart caddy
echo "setup done"
