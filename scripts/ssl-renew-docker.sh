#!/usr/bin/env bash
# Renew Let's Encrypt certs using the same Docker flow as production-ssl-certbot.sh (no certbot .deb required).
#
# Run as root (cron). DOMAIN must match the directory name under /etc/letsencrypt/live/ (usually your apex).
#
# Usage:
#   sudo ./scripts/ssl-renew-docker.sh
#   sudo DOMAIN=redwood-plus.fr ./scripts/ssl-renew-docker.sh
#
# Cron (root), e.g. daily at 03:15:
#   15 3 * * * /opt/redwood/scripts/ssl-renew-docker.sh >> /var/log/redwood-ssl-renew.log 2>&1

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="${DOMAIN:-redwood-plus.fr}"
WEBROOT="${REPO_ROOT}/nginx/acme-webroot"
CERT_DIR="${REPO_ROOT}/nginx/certs"
COMPOSE_DIR="${REPO_ROOT}/docker"
LE_DIR="${LE_DIR:-/etc/letsencrypt}"

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Run as root (sudo)."
  exit 1
fi

mkdir -p "$WEBROOT/.well-known/acme-challenge"

echo "==> certbot renew (Docker)"
docker run --rm \
  -v "$WEBROOT:/var/www/certbot:rw" \
  -v "$LE_DIR:/etc/letsencrypt" \
  certbot/certbot:latest renew \
  --webroot -w /var/www/certbot \
  --non-interactive

LIVE="$LE_DIR/live/$DOMAIN"
if [[ ! -f "$LIVE/fullchain.pem" || ! -f "$LIVE/privkey.pem" ]]; then
  echo "Missing $LIVE/*.pem — check DOMAIN= matches /etc/letsencrypt/live/"
  exit 1
fi

echo "==> Sync PEMs to $CERT_DIR and reload nginx"
install -m 0644 "$LIVE/fullchain.pem" "$CERT_DIR/fullchain.pem"
install -m 0640 "$LIVE/privkey.pem" "$CERT_DIR/privkey.pem"

if [[ -f "$COMPOSE_DIR/docker-compose.yml" ]]; then
  (cd "$COMPOSE_DIR" && docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload)
fi

echo "==> Done"
