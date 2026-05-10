#!/usr/bin/env bash
# Run on the VPS after `git pull` (e.g. from GitHub Actions SSH step).
# Rebuilds API/worker images and restarts the stack. Does not touch Postgres/Redis data volumes.
#
# Optional env:
#   DEPLOY_ROOT  — repo root (default: parent of scripts/)
#   DEPLOY_BRANCH — unused here; checkout/pull is expected to be done by the caller
#   USE_SUDO_DOCKER=1 — prefix docker with sudo (typical if the SSH user is not in the docker group)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_ROOT="${DEPLOY_ROOT:-$REPO_ROOT}"
COMPOSE_DIR="${DEPLOY_ROOT}/docker"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Missing $COMPOSE_FILE (DEPLOY_ROOT=$DEPLOY_ROOT)"
  exit 1
fi

docker_compose() {
  if [[ "${USE_SUDO_DOCKER:-0}" == "1" ]]; then
    sudo docker compose -f "$COMPOSE_FILE" "$@"
  else
    docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

echo "==> Building images (api, worker, beat, flower)"
docker_compose build api worker beat flower

echo "==> Recreating services"
docker_compose up -d --remove-orphans

echo "==> Deploy finished. If HTTPS broke after a pull, run: ./scripts/apply-nginx-ssl-config.sh <domain> [www]"
