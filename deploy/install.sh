#!/usr/bin/env bash
# Idempotent deploy for the RAID Architect web app on an Abacus supercomputer.
# Re-run after an image update to recreate the systemd unit + nginx vhost symlinks.
set -euo pipefail

PROJECT_DIR="/home/ubuntu/RAID-disk-pricer"
PY="/opt/abacus-python/bin/python3"
PIP="/opt/abacus-python/bin/pip"
SERVICE="raid-web"
VHOST="24ff2001c.conf"

cd "$PROJECT_DIR"

echo "==> Installing web dependencies into /opt/abacus-python"
"$PIP" install -q flask gunicorn

echo "==> Linking systemd unit"
sudo ln -sf "$PROJECT_DIR/deploy/${SERVICE}.service" "/etc/systemd/system/${SERVICE}.service"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sudo systemctl restart "$SERVICE"

echo "==> Linking nginx vhost"
sudo ln -sf "$PROJECT_DIR/deploy/${VHOST}" "/etc/nginx/conf.d/${VHOST}"
sudo nginx -t
sudo systemctl reload nginx

echo "==> Done. Service status:"
systemctl --no-pager --lines=0 status "$SERVICE" || true
