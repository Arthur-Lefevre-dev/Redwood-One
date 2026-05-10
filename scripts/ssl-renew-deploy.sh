#!/usr/bin/env bash
# Certbot --deploy-hook: copy renewed certs into the repo and reload nginx.
# Only runs when certbot is installed ON THE HOST (apt install certbot).
#
# If you do NOT have certbot on the host, use ssl-renew-docker.sh instead (cron that script).
#
# Example crontab (root), with system certbot:
#   0 3 * * * certbot renew --webroot -w /path/to/repo/nginx/acme-webroot --deploy-hook /path/to/repo/scripts/ssl-renew-deploy.sh
#
# Certbot sets RENEWED_LINEAGE to e.g. /etc/letsencrypt/live/example.com

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="${REPO_ROOT}/nginx/certs"
COMPOSE_DIR="${REPO_ROOT}/docker"

if [[ -z "${RENEWED_LINEAGE:-}" ]]; then
  echo "ssl-renew-deploy: RENEWED_LINEAGE empty; skipping"
  exit 0
fi

install -m 0644 "$RENEWED_LINEAGE/fullchain.pem" "$CERT_DIR/fullchain.pem"
install -m 0640 "$RENEWED_LINEAGE/privkey.pem" "$CERT_DIR/privkey.pem"

if [[ -f "$COMPOSE_DIR/docker-compose.yml" ]]; then
  (cd "$COMPOSE_DIR" && docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload)
fi

echo "ssl-renew-deploy: updated $CERT_DIR and reloaded nginx"
