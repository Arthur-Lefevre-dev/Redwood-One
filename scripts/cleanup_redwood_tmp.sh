#!/usr/bin/env bash
# Remove stale Redwood temp data under /tmp/redwood (Docker volume tmp_data).
# Safe targets after a crash: torrent aria2 job dirs, optional .torrent blobs, optional upload staging.
#
# Run on the VPS (from docker/):
#   docker compose exec worker bash /app/scripts/cleanup_redwood_tmp.sh --yes --all
# If worker is not running, use a one-off container (same image + tmp volume):
#   docker compose run --rm worker bash /app/scripts/cleanup_redwood_tmp.sh --dry-run --all
# Dry-run with exec (requires worker up):
#   docker compose exec worker bash /app/scripts/cleanup_redwood_tmp.sh --dry-run --all
#
# Stop active encodes/uploads before using --uploads or --all (otherwise you may delete in-use files).

set -euo pipefail

ROOT="${REDWOOD_TMP_ROOT:-/tmp/redwood}"
TORRENTS="${ROOT}/torrents"
BLOBS="${ROOT}/torrent_blobs"
UPLOADS="${ROOT}/uploads"

DO_JOBS=0
DO_BLOBS=0
DO_UPLOADS=0
DRY=0
YES=0

usage() {
  echo "Usage: $0 [--jobs] [--blobs] [--uploads] [--all] [--yes] [--dry-run]" >&2
  echo "  --jobs     Remove ${TORRENTS}/job_* (aria2 download dirs; default if no scope flag)." >&2
  echo "  --blobs    Remove ${BLOBS}/*.torrent (admin .torrent sources; DB may still reference paths)." >&2
  echo "  --uploads  Remove all files under ${UPLOADS}/ (staging + partial outputs)." >&2
  echo "  --all      jobs + blobs + uploads." >&2
  echo "  --yes      Required for --blobs, --uploads, and --all (safety)." >&2
  echo "  --dry-run  Print actions only." >&2
}

log() { echo "[cleanup_redwood_tmp] $*"; }

rm_path() {
  local p="$1"
  if [[ ! -e "$p" ]]; then
    log "skip (missing): $p"
    return 0
  fi
  if [[ "$DRY" -eq 1 ]]; then
    log "dry-run: would remove: $p"
    return 0
  fi
  rm -rf "$p"
  log "removed: $p"
}

clean_jobs() {
  shopt -s nullglob
  local d
  for d in "${TORRENTS}/job_"*; do
    [[ -d "$d" ]] || continue
    rm_path "$d"
  done
  shopt -u nullglob
}

clean_blobs() {
  mkdir -p "$BLOBS" 2>/dev/null || true
  shopt -s nullglob
  local f
  for f in "${BLOBS}/"*.torrent; do
    [[ -f "$f" ]] || continue
    rm_path "$f"
  done
  shopt -u nullglob
}

clean_uploads() {
  mkdir -p "$UPLOADS" 2>/dev/null || true
  [[ -d "$UPLOADS" ]] || return 0
  find "$UPLOADS" -mindepth 1 -maxdepth 1 -print0 | while IFS= read -r -d '' p; do
    rm_path "$p"
  done
}

SCOPE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jobs) DO_JOBS=1; SCOPE=1 ;;
    --blobs) DO_BLOBS=1; SCOPE=1 ;;
    --uploads) DO_UPLOADS=1; SCOPE=1 ;;
    --all) DO_JOBS=1; DO_BLOBS=1; DO_UPLOADS=1; SCOPE=1 ;;
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if [[ "$SCOPE" -eq 0 ]]; then
  DO_JOBS=1
fi

if [[ "$DO_BLOBS" -eq 1 || "$DO_UPLOADS" -eq 1 ]]; then
  if [[ "$YES" -ne 1 && "$DRY" -ne 1 ]]; then
    echo "Refus: ajoute --yes pour confirmer --blobs / --uploads / --all (ou --dry-run pour simuler)." >&2
    exit 3
  fi
fi

log "ROOT=$ROOT dry_run=$DRY jobs=$DO_JOBS blobs=$DO_BLOBS uploads=$DO_UPLOADS"

if [[ "$DO_JOBS" -eq 1 ]]; then
  mkdir -p "$TORRENTS" 2>/dev/null || true
  clean_jobs
fi
if [[ "$DO_BLOBS" -eq 1 ]]; then
  clean_blobs
fi
if [[ "$DO_UPLOADS" -eq 1 ]]; then
  clean_uploads
fi

log "done."
