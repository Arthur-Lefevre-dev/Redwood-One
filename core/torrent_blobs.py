"""Shared paths for persisted .torrent files (API writes, worker reads for retries)."""

from pathlib import Path

TORRENT_BLOBS_DIR = Path("/tmp/redwood/torrent_blobs")


def ensure_torrent_blobs_dir() -> None:
    TORRENT_BLOBS_DIR.mkdir(parents=True, exist_ok=True)


def torrent_blob_path_for_film_id(film_id: int) -> Path:
    return TORRENT_BLOBS_DIR / f"{int(film_id)}.torrent"
