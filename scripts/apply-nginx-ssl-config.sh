#!/usr/bin/env bash
# Apply nginx.conf from nginx.conf.ssl.template when certs already exist (e.g. after git pull
# overwrote nginx.conf, or manual recovery). Does not call certbot.
#
# Usage (from repo root, user in docker group or via sudo):
#   ./scripts/apply-nginx-ssl-config.sh redwood-plus.fr
#   ./scripts/apply-nginx-ssl-config.sh redwood-plus.fr www   # if cert includes www

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_ROOT}/nginx/nginx.conf.ssl.template"
NGINX_CONF="${REPO_ROOT}/nginx/nginx.conf"
COMPOSE_FILE="${REPO_ROOT}/docker/docker-compose.yml"

DOMAIN="${1:-}"
EXTRA="${2:-}"
SERVER_NAMES="$DOMAIN"
case "${EXTRA}" in
  www|WWW|1|yes|true) SERVER_NAMES="$DOMAIN www.$DOMAIN" ;;
esac

if [[ -z "$DOMAIN" ]]; then
  echo "Usage: $0 <domain> [www]"
  exit 1
fi
[[ -f "$TEMPLATE" ]] || { echo "Missing $TEMPLATE"; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { echo "Missing $COMPOSE_FILE"; exit 1; }

BACKUP="${NGINX_CONF}.bak.$(date +%Y%m%d%H%M%S)"
if [[ -f "$NGINX_CONF" ]]; then
  cp -a "$NGINX_CONF" "$BACKUP"
  echo "Backed up nginx.conf to $BACKUP"
fi

echo "Writing HTTPS nginx.conf (server_name: $SERVER_NAMES)"
sed -e "s/__SERVER_NAMES__/${SERVER_NAMES}/g" "$TEMPLATE" >"${NGINX_CONF}.tmp"
mv "${NGINX_CONF}.tmp" "$NGINX_CONF"

(cd "$(dirname "$COMPOSE_FILE")" && docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload)
echo "Done. Verify: sudo docker exec redwood_nginx nginx -T 2>/dev/null | grep -E '^\\s*listen'"
