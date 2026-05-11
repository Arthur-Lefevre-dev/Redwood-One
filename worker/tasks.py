"""Celery worker tasks: torrent download + unified film pipeline."""

import base64
import binascii
import logging
import shutil
from pathlib import Path
from typing import Optional

from celery import Celery
from celery.schedules import crontab

from config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
app = Celery(
    "redwood",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "refresh-donation-balances-hourly": {
            "task": "worker.tasks.refresh_donation_balances_snapshot",
            "schedule": crontab(minute=0),
        },
    },
    # Celery 5.3+: explicit startup retry; broker_connection_retry alone is deprecated for this.
    broker_connection_retry_on_startup=True,
)


VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}
TORRENT_DIR = Path("/tmp/redwood/torrents")


def _find_video_file(root: Path) -> Optional[str]:
    if root.is_file() and root.suffix.lower() in VIDEO_EXTS:
        return str(root)
    best: Optional[Path] = None
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            if best is None or p.stat().st_size > best.stat().st_size:
                best = p
    return str(best) if best else None


def _persist_torrent_stats(film_id: int, stats: dict) -> None:
    from db.models import Film
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        f = db.get(Film, film_id)
        if not f:
            return
        f.torrent_stats = stats
        total = stats.get("total_bytes") or 0
        done = stats.get("completed_bytes") or 0
        if total > 0:
            f.pipeline_progress = max(1, min(99, int(100 * done / total)))
        db.commit()
    finally:
        db.close()


@app.task(name="worker.tasks.download_torrent_task", bind=True, max_retries=0)
def download_torrent_task(
    self,
    film_id: int,
    magnet: Optional[str] = None,
    torrent_b64: Optional[str] = None,
):
    """Download torrent via aria2 RPC, update live stats in DB, then process_film_task."""
    logger.info(
        "download_torrent_task start film_id=%s magnet=%s torrent_b64_len=%s",
        film_id,
        bool(magnet),
        len(torrent_b64) if torrent_b64 else 0,
    )
    if not shutil.which("aria2c"):
        _fail_film(film_id, "aria2c not installed in worker image")
        return

    torrent_bytes: Optional[bytes] = None
    if torrent_b64:
        try:
            torrent_bytes = base64.b64decode(torrent_b64, validate=True)
        except (ValueError, binascii.Error):
            _fail_film(film_id, "invalid base64 torrent payload from queue")
            return
        if len(torrent_bytes) < 64:
            _fail_film(film_id, ".torrent payload too small or empty after decode")
            return

    from core.torrent_aria import download_magnet_or_torrent

    TORRENT_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = TORRENT_DIR / f"job_{film_id}"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        download_magnet_or_torrent(
            job_dir,
            magnet=magnet,
            torrent_bytes=torrent_bytes,
            on_poll=lambda s: _persist_torrent_stats(film_id, s),
            poll_interval=1.5,
            deadline_sec=86400,
        )
    except Exception as e:
        logger.exception("torrent download film_id=%s", film_id)
        _fail_film(film_id, str(e)[:8000])
        return

    video = _find_video_file(job_dir)
    if not video:
        _fail_film(film_id, "no video file found after torrent download")
        return

    from db.models import Film
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        f = db.get(Film, film_id)
        if f:
            f.titre = Path(video).name
            f.pipeline_progress = 10
            f.torrent_stats = None
            db.commit()
    finally:
        db.close()

    process_film_task.delay(film_id, video)


def _fail_film(film_id: int, message: str) -> None:
    from db.models import Film, FilmStatut
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        f = db.get(Film, film_id)
        if f:
            f.statut = FilmStatut.erreur
            f.erreur_message = message[:8000]
            f.torrent_stats = None
            db.commit()
    finally:
        db.close()


@app.task(name="worker.tasks.process_film_task", bind=True, max_retries=0)
def process_film_task(self, film_id: int, local_path: str):
    from core.pipeline import process_film_file
    from db.models import Film
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        db.query(Film).filter(Film.id == film_id).update({"torrent_stats": None})
        db.commit()
        film = db.get(Film, film_id)
        if not film:
            return

        def prog(p: int) -> None:
            # Own session per update: safe if progress callbacks run from a ffmpeg stderr thread.
            s2 = SessionLocal()
            try:
                s2.query(Film).filter(Film.id == film_id).update({"pipeline_progress": p})
                s2.commit()
            finally:
                s2.close()

        process_film_file(db, film, local_path, progress=prog)
    finally:
        db.close()


@app.task(name="worker.tasks.vast_transcode_test_task", bind=True, max_retries=0)
def vast_transcode_test_task(self, job_token: str, src_ext: str, offer_id: Optional[int] = None):
    """Transcode one file on a Vast GPU instance (S3 presigned URLs + onstart). Costs Vast rental until destroyed."""
    from core.vast_remote_transcode import run_vast_transcode_test

    return run_vast_transcode_test(self, job_token, src_ext, offer_id)


@app.task(name="worker.tasks.refresh_donation_balances_snapshot")
def refresh_donation_balances_snapshot() -> None:
    """Refresh cached crypto balances + EUR snapshot for donation bar (CoinGecko + chain RPC)."""
    from datetime import datetime

    from core.donation_service import compute_donation_snapshot
    from core.donation_settings_store import get_or_create_donation_settings
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        row = get_or_create_donation_settings(db)
        addresses = {
            "btc": row.address_btc,
            "polygon": row.address_polygon,
            "solana": row.address_solana,
            "xrp": row.address_xrp,
            "tron": row.address_tron,
        }
        if not any((v or "").strip() for v in addresses.values()):
            logger.info("refresh_donation_balances_snapshot: no addresses configured, skip")
            return
        try:
            snap = compute_donation_snapshot(addresses)
        except ValueError as exc:
            logger.warning("refresh_donation_balances_snapshot: %s", exc)
            return
        except Exception:
            logger.exception("refresh_donation_balances_snapshot: chain/API error")
            return
        row.snapshot_json = snap
        row.updated_at = datetime.utcnow()
        db.commit()
        logger.info("refresh_donation_balances_snapshot: ok raised_eur=%s", snap.get("raised_eur"))
    finally:
        db.close()
