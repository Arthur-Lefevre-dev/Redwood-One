#!/usr/bin/env bash
# Obtain a Let's Encrypt certificate (HTTP-01 webroot) and enable HTTPS in nginx for Docker production.
#
# Prerequisites (on the Linux server):
#   - Docker and Docker Compose plugin
#   - DNS A/AAAA records for DOMAIN (and www if WWW=1) pointing to this host
#   - Ports 80 and 443 reachable from the Internet
#   - Stack running so nginx serves /.well-known/acme-challenge/ (see nginx.conf + docker-compose volume)
#
# Usage:
#   export DOMAIN=redwood-plus.fr
#   export EMAIL=admin@example.com
#   # optional: also request www and add it to server_name
#   export WWW=1
#   sudo ./scripts/production-ssl-certbot.sh
#
# Or one line:
#   sudo DOMAIN=redwood-plus.fr EMAIL=you@domain.tld ./scripts/production-ssl-certbot.sh
#
# After success, add a cron job for renewal, e.g. twice daily:
#   0 3,15 * * * certbot renew --webroot -w /opt/Redwood-One/nginx/acme-webroot --deploy-hook /opt/Redwood-One/scripts/ssl-renew-deploy.sh
# (adjust paths; deploy-hook copies certs and reloads nginx)

set -euo pipefail

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
WWW="${WWW:-0}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBROOT="${REPO_ROOT}/nginx/acme-webroot"
CERT_DIR="${REPO_ROOT}/nginx/certs"
TEMPLATE="${REPO_ROOT}/nginx/nginx.conf.ssl.template"
NGINX_CONF="${REPO_ROOT}/nginx/nginx.conf"
COMPOSE_FILE="${REPO_ROOT}/docker/docker-compose.yml"
LE_DIR="${LE_DIR:-/etc/letsencrypt}"

usage() {
  echo "Usage: sudo DOMAIN=example.com EMAIL=you@mail.tld $0"
  echo "Optional: WWW=1 to include www.\$DOMAIN in the certificate and server_name."
  exit 1
}

[[ -n "$DOMAIN" && -n "$EMAIL" ]] || usage
[[ -d "$REPO_ROOT/nginx" ]] || { echo "Cannot find repo nginx/ under $REPO_ROOT"; exit 1; }
[[ -f "$TEMPLATE" ]] || { echo "Missing $TEMPLATE"; exit 1; }

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Run as root (sudo) so certbot can write to $LE_DIR and copy private keys."
  exit 1
fi

mkdir -p "$WEBROOT/.well-known/acme-challenge" "$CERT_DIR"
chmod 755 "$WEBROOT" "$WEBROOT/.well-known" "$WEBROOT/.well-known/acme-challenge"

SERVER_NAMES="$DOMAIN"
CERTBOT_DOMAINS=(-d "$DOMAIN")
if [[ "$WWW" == "1" ]]; then
  SERVER_NAMES="$DOMAIN www.$DOMAIN"
  CERTBOT_DOMAINS+=(-d "www.$DOMAIN")
fi

echo "==> Ensuring nginx is up (ACME webroot must be reachable on port 80)"
if [[ -f "$COMPOSE_FILE" ]]; then
  (cd "$(dirname "$COMPOSE_FILE")" && docker compose up -d nginx)
else
  echo "Warning: $COMPOSE_FILE not found; start nginx yourself so port 80 serves the webroot."
fi

echo "==> Requesting certificate for: ${CERTBOT_DOMAINS[*]}"
docker run --rm \
  -v "$WEBROOT:/var/www/certbot:rw" \
  -v "$LE_DIR:/etc/letsencrypt" \
  certbot/certbot:latest certonly \
  --webroot -w /var/www/certbot \
  "${CERTBOT_DOMAINS[@]}" \
  -m "$EMAIL" \
  --agree-tos \
  --non-interactive \
  --keep-until-expiring

LIVE="$LE_DIR/live/$DOMAIN"
[[ -f "$LIVE/fullchain.pem" && -f "$LIVE/privkey.pem" ]] || {
  echo "Expected files missing under $LIVE"
  exit 1
}

echo "==> Installing PEM files into $CERT_DIR"
install -m 0644 "$LIVE/fullchain.pem" "$CERT_DIR/fullchain.pem"
install -m 0640 "$LIVE/privkey.pem" "$CERT_DIR/privkey.pem"

BACKUP="${NGINX_CONF}.pre-ssl.$(date +%Y%m%d%H%M%S)"
if [[ -f "$NGINX_CONF" ]]; then
  cp -a "$NGINX_CONF" "$BACKUP"
  echo "==> Backed up current nginx.conf to $BACKUP"
fi

echo "==> Writing HTTPS nginx.conf (server_name: $SERVER_NAMES)"
sed -e "s/__SERVER_NAMES__/${SERVER_NAMES}/g" "$TEMPLATE" >"$NGINX_CONF.tmp"
mv "$NGINX_CONF.tmp" "$NGINX_CONF"

echo "==> Reloading nginx container"
if [[ -f "$COMPOSE_FILE" ]]; then
  (cd "$(dirname "$COMPOSE_FILE")" && docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload)
else
  echo "Run manually: docker compose -f docker/docker-compose.yml exec nginx nginx -t && docker compose exec nginx nginx -s reload"
fi

echo ""
echo "Done. HTTPS should be active for: $SERVER_NAMES"
echo "Renewal: use certbot renew with --webroot -w $WEBROOT"
echo "Optional deploy hook: $REPO_ROOT/scripts/ssl-renew-deploy.sh"
