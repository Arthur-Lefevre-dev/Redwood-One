"""Celery worker tasks: torrent download + unified film pipeline."""

import base64
import binascii
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

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
        "torrent-auto-retry-scan": {
            "task": "worker.tasks.torrent_auto_retry_scan",
            "schedule": crontab(minute="*/2"),
        },
    },
    # Celery 5.3+: explicit startup retry; broker_connection_retry alone is deprecated for this.
    broker_connection_retry_on_startup=True,
)


VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}
TORRENT_DIR = Path("/tmp/redwood/torrents")


def _set_film_pipeline_task(film_id: int, task_id: Optional[str], kind: Optional[str]) -> None:
    """Persist active Celery task id for admin cancel (English comments in worker)."""
    from db.models import Film
    from db.session import SessionLocal

    db = SessionLocal()
    try:
        f = db.get(Film, film_id)
        if f:
            f.pipeline_celery_task_id = task_id
            f.pipeline_celery_task_kind = kind
            db.commit()
    finally:
        db.close()


def _find_video_file(root: Path) -> Optional[str]:
    if root.is_file() and root.suffix.lower() in VIDEO_EXTS:
        return str(root)
    best: Optional[Path] = None
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            if best is None or p.stat().st_size > best.stat().st_size:
                best = p
    return str(best) if best else None


def _find_all_video_paths_sorted(job_dir: Path) -> List[str]:
    """All video files under the torrent job directory, sorted by relative path."""
    paths: List[str] = []
    if job_dir.is_file() and job_dir.suffix.lower() in VIDEO_EXTS:
        return [str(job_dir)]
    for p in job_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            paths.append(str(p))
    paths.sort(key=lambda s: Path(s).as_posix().lower())
    return paths


def _pick_torrent_video_paths_for_content_kind(kind: object, paths: List[str]) -> List[str]:
    """
    For series_episode, keep every video file. For film (or unknown), keep a single file
    (largest) when the torrent contains multiple videos.
    """
    from db.models import ContentKind

    if not paths:
        return paths
    if kind == ContentKind.series_episode or str(kind).split(".")[-1] == "series_episode":
        return paths
    if len(paths) == 1:
        return paths
    largest = max(paths, key=lambda fp: Path(fp).stat().st_size)
    logger.info(
        "torrent multi-file non-series: using largest video only (%s)",
        Path(largest).name,
    )
    return [largest]


def _series_pack_rows(
    db,
    parent_id: int,
    video_paths: List[str],
) -> List[Tuple[int, str]]:
    """
    Map each video path to a Film id. Parent row absorbs episode 0; extra DB rows for the rest.
    Caller must commit after this returns (this function commits).
    """
    from db.models import ContentKind, Film, FilmSource, FilmStatut

    parent = db.get(Film, int(parent_id))
    if not parent:
        return []
    first = video_paths[0]
    parent.titre = Path(first).name
    parent.pipeline_progress = 10
    parent.torrent_stats = None
    out: List[Tuple[int, str]] = [(int(parent_id), first)]
    for vp in video_paths[1:]:
        child = Film(
            titre=Path(vp).name,
            source=FilmSource.torrent,
            statut=FilmStatut.en_cours,
            pipeline_progress=10,
            content_kind=ContentKind.series_episode,
            transcode_target=parent.transcode_target,
            vast_offer_id=parent.vast_offer_id,
        )
        db.add(child)
        db.flush()
        out.append((int(child.id), vp))
    db.commit()
    return out


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
    """Download torrent via aria2 RPC, update live stats in DB, then process_film_task or Vast."""
    from db.models import Film
    from db.session import SessionLocal

    db_load = SessionLocal()
    try:
        row = db_load.get(Film, film_id)
        if not row:
            logger.warning("download_torrent_task: film_id=%s not found", film_id)
            return
        if magnet is None and torrent_b64 is None:
            m = (getattr(row, "torrent_magnet_uri", None) or "").strip()
            if m:
                magnet = m
            bp = (getattr(row, "torrent_blob_path", None) or "").strip()
            if bp and os.path.isfile(bp):
                with open(bp, "rb") as fh:
                    torrent_b64 = base64.b64encode(fh.read()).decode("ascii")
    finally:
        db_load.close()

    logger.info(
        "download_torrent_task start film_id=%s magnet=%s torrent_b64_len=%s",
        film_id,
        bool(magnet),
        len(torrent_b64) if torrent_b64 else 0,
    )
    if not magnet and not torrent_b64:
        _fail_film(
            film_id,
            "Torrent: aucune source (magnet ou fichier .torrent) enregistrée pour ce film.",
        )
        return

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

    all_paths = _find_all_video_paths_sorted(job_dir)
    if not all_paths:
        _fail_film(film_id, "no video file found after torrent download")
        return

    from celery import chain

    from db.models import ContentKind, Film, FilmTraitement
    from db.session import SessionLocal

    db = SessionLocal()
    target = "local"
    vast_oid: Optional[int] = None
    pack: List[Tuple[int, str]] = []
    try:
        parent = db.get(Film, film_id)
        if not parent:
            _fail_film(film_id, "film row missing after torrent download")
            return
        target = (parent.transcode_target or "local").lower().strip()
        if target not in ("local", "vast"):
            target = "local"
        vast_oid = parent.vast_offer_id
        paths = _pick_torrent_video_paths_for_content_kind(parent.content_kind, all_paths)
        if not paths:
            _fail_film(film_id, "no video file found after torrent download")
            return
        is_series = parent.content_kind == ContentKind.series_episode
        if is_series and len(paths) > 1:
            pack = _series_pack_rows(db, film_id, paths)
            logger.info(
                "torrent series pack parent_film_id=%s episode_files=%s",
                film_id,
                len(pack),
            )
        else:
            first = paths[0]
            parent.titre = Path(first).name
            parent.pipeline_progress = 10
            parent.torrent_stats = None
            db.commit()
            pack = [(int(film_id), first)]
    finally:
        db.close()

    if target == "vast":
        if not (settings.VAST_API_KEY or "").strip():
            _fail_film(
                film_id,
                "VAST_API_KEY manquante : impossible d’exécuter le transcodage Vast après le torrent.",
            )
            return
        import uuid as uuid_mod

        from core.s3 import upload_file

        uploads: List[Tuple[int, str, str, str]] = []
        try:
            for fid, vpath in pack:
                ext = Path(vpath).suffix.lower()
                if ext not in VIDEO_EXTS:
                    _fail_film(
                        int(fid),
                        f"Extension non prise en charge pour Vast après torrent : {ext}",
                    )
                    return
                job_token = uuid_mod.uuid4().hex
                s3_in = f"vast-test/{job_token}/input{ext}"
                upload_file(vpath, s3_in)
                try:
                    os.remove(vpath)
                except OSError:
                    pass
                uploads.append((int(fid), job_token, ext, Path(vpath).name))
        except Exception as e:
            logger.exception("torrent->vast S3 upload film_id=%s", film_id)
            _fail_film(film_id, str(e)[:8000])
            return
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        oid = int(vast_oid) if vast_oid is not None and int(vast_oid) > 0 else None
        sigs = [
            vast_transcode_test_task.si(jt, ext, oid, fid, title, "torrent_vast")
            for fid, jt, ext, title in uploads
        ]
        try:
            async_res = chain(*sigs).delay()
        except Exception as e:
            logger.exception("torrent->vast Celery enqueue film_id=%s", film_id)
            _fail_film(film_id, str(e)[:8000])
            return
        db2 = SessionLocal()
        try:
            head = True
            for fid, jt, ext, title in uploads:
                f2 = db2.get(Film, fid)
                if f2:
                    f2.pipeline_progress = 12
                    f2.traitement = FilmTraitement.transcode
                    f2.vast_pending_job_token = jt
                    f2.vast_pending_input_ext = ext
                    if head:
                        f2.pipeline_celery_task_id = async_res.id
                        f2.pipeline_celery_task_kind = "vast_transcode"
                        head = False
                    else:
                        f2.pipeline_celery_task_id = None
                        f2.pipeline_celery_task_kind = None
            db2.commit()
        finally:
            db2.close()
        return

    sigs = [process_film_task.si(int(fid), str(p)) for fid, p in pack]
    try:
        async_res = chain(*sigs).delay()
    except Exception as e:
        logger.exception("torrent->local Celery enqueue film_id=%s", film_id)
        _fail_film(film_id, str(e)[:8000])
        return
    _set_film_pipeline_task(
        int(film_id),
        async_res.id,
        "process_series_pack" if len(pack) > 1 else "process_film",
    )


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
            f.pipeline_celery_task_id = None
            f.pipeline_celery_task_kind = None
            db.commit()
    finally:
        db.close()


@app.task(name="worker.tasks.torrent_auto_retry_scan")
def torrent_auto_retry_scan() -> None:
    """Periodic scan: re-queue torrent downloads that failed with a transient error."""
    from sqlalchemy import func

    from core.torrent_auto_retry import torrent_error_eligible_for_auto_retry
    from db.models import Film, FilmSource, FilmStatut
    from db.session import SessionLocal

    s = get_settings()
    if not s.TORRENT_AUTO_RETRY_ENABLED:
        return
    max_retries = max(0, int(s.TORRENT_AUTO_RETRY_MAX))
    if max_retries <= 0:
        return

    db = SessionLocal()
    try:
        rows = (
            db.query(Film)
            .filter(
                Film.source == FilmSource.torrent,
                Film.statut == FilmStatut.erreur,
                func.coalesce(Film.torrent_auto_retry_count, 0) < max_retries,
            )
            .order_by(Film.id.asc())
            .limit(25)
            .all()
        )
    finally:
        db.close()

    for f in rows:
        if not torrent_error_eligible_for_auto_retry((f.erreur_message or "").strip()):
            continue
        has_magnet = bool((f.torrent_magnet_uri or "").strip())
        bp = (f.torrent_blob_path or "").strip()
        if not has_magnet and not (bp and os.path.isfile(bp)):
            continue

        prev = 0
        db3 = SessionLocal()
        try:
            row = db3.get(Film, f.id)
            if (
                not row
                or row.source != FilmSource.torrent
                or row.statut != FilmStatut.erreur
            ):
                continue
            prev = int(row.torrent_auto_retry_count or 0)
            if prev >= max_retries:
                continue
            row.statut = FilmStatut.en_cours
            row.pipeline_progress = 0
            row.erreur_message = None
            row.torrent_stats = None
            row.pipeline_celery_task_id = None
            row.pipeline_celery_task_kind = None
            row.torrent_auto_retry_count = prev + 1
            db3.commit()
        except Exception:
            logger.exception("torrent_auto_retry_scan: commit film_id=%s", f.id)
            db3.rollback()
            continue
        finally:
            db3.close()

        try:
            download_torrent_task.delay(f.id)
            logger.info(
                "torrent_auto_retry_scan: re-queued film_id=%s attempt=%s/%s",
                f.id,
                prev + 1,
                max_retries,
            )
        except Exception:
            logger.exception("torrent_auto_retry_scan: enqueue download_torrent_task film_id=%s", f.id)
            db4 = SessionLocal()
            try:
                r2 = db4.get(Film, f.id)
                if r2:
                    r2.statut = FilmStatut.erreur
                    r2.erreur_message = (
                        "Relance auto : impossible d’envoyer la tâche Celery (Redis indisponible ?)"
                    )[:8000]
                    r2.torrent_auto_retry_count = max(0, prev)
                    db4.commit()
            finally:
                db4.close()


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
        s3 = SessionLocal()
        try:
            s3.query(Film).filter(Film.id == film_id).update(
                {
                    "pipeline_celery_task_id": None,
                    "pipeline_celery_task_kind": None,
                }
            )
            s3.commit()
        finally:
            s3.close()


@app.task(name="worker.tasks.vast_transcode_test_task", bind=True, max_retries=0, track_started=True)
def vast_transcode_test_task(
    self,
    job_token: str,
    src_ext: str,
    offer_id: Optional[int] = None,
    film_id: Optional[int] = None,
    film_title: Optional[str] = None,
    envelope_source: str = "admin",
):
    """Transcode one file on a Vast GPU instance (S3 presigned URLs + onstart). Costs Vast rental until destroyed."""
    from core.vast_film_finalize import mark_film_vast_task_failed
    from core.vast_remote_transcode import run_vast_transcode_test
    from core.vast_transcode_cancel import store_job_envelope

    tid = str(getattr(self.request, "id", "") or "").strip()
    if tid and film_id is not None and int(film_id) > 0:
        store_job_envelope(
            tid,
            job_token,
            src_ext,
            film_id=int(film_id),
            film_title=(film_title or "").strip() or None,
            source=(envelope_source or "admin")[:64],
        )
    try:
        return run_vast_transcode_test(self, job_token, src_ext, offer_id, film_id=film_id)
    except Exception as e:
        if film_id is not None and int(film_id) > 0:
            mark_film_vast_task_failed(int(film_id), str(e))
        raise


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
