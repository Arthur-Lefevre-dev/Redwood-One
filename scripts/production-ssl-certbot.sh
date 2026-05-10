#!/usr/bin/env bash
# Obtain a Let's Encrypt certificate (HTTP-01 webroot) and enable HTTPS in nginx for Docker production.
#
# Prerequisites (on the Linux server):
#   - Docker and Docker Compose plugin
#   - DNS A/AAAA records for DOMAIN (and www if requested) pointing to this host
#   - Ports 80 and 443 reachable from the Internet
#   - Stack running so nginx serves /.well-known/acme-challenge/ (see nginx.conf + docker-compose volume)
#
# Usage (recommended — arguments are not stripped by sudo on Debian):
#   cd /opt/redwood
#   chmod +x scripts/production-ssl-certbot.sh
#   sudo ./scripts/production-ssl-certbot.sh redwood-plus.fr contact@redwood.fr
#   # optional 3rd arg "www" to include www.<domain> in cert + server_name:
#   sudo ./scripts/production-ssl-certbot.sh redwood-plus.fr contact@redwood.fr www
#
# If you are already in scripts/:
#   sudo ./production-ssl-certbot.sh redwood-plus.fr contact@redwood.fr
#
# With environment variables you must preserve them through sudo:
#   sudo -E ./scripts/production-ssl-certbot.sh
#   # or:
#   sudo env DOMAIN=redwood-plus.fr EMAIL=contact@redwood.fr ./scripts/production-ssl-certbot.sh
#
# After success, add a cron job for renewal, e.g. twice daily:
#   0 3,15 * * * certbot renew --webroot -w /opt/redwood/nginx/acme-webroot --deploy-hook /opt/redwood/scripts/ssl-renew-deploy.sh
# (adjust paths; deploy-hook copies certs and reloads nginx)

set -euo pipefail

DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-}"
WWW="${WWW:-0}"

# Positional args (work reliably with sudo; env vars are often dropped by sudo's env_reset)
if [[ $# -ge 2 ]]; then
  DOMAIN="$1"
  EMAIL="$2"
  case "${3:-}" in
    www|WWW|1|yes|true) WWW=1 ;;
  esac
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBROOT="${REPO_ROOT}/nginx/acme-webroot"
CERT_DIR="${REPO_ROOT}/nginx/certs"
TEMPLATE="${REPO_ROOT}/nginx/nginx.conf.ssl.template"
NGINX_CONF="${REPO_ROOT}/nginx/nginx.conf"
COMPOSE_FILE="${REPO_ROOT}/docker/docker-compose.yml"
LE_DIR="${LE_DIR:-/etc/letsencrypt}"

usage() {
  echo "Usage:"
  echo "  sudo $0 <domain> <email> [www]"
  echo "Example (from repo root $REPO_ROOT):"
  echo "  cd \"$REPO_ROOT\""
  echo "  sudo ./scripts/production-ssl-certbot.sh redwood-plus.fr contact@example.fr"
  echo ""
  echo "Or with env (use sudo -E or sudo env ...):"
  echo "  sudo env DOMAIN=example.com EMAIL=you@mail.tld $0"
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
