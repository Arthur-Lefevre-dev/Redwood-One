"""Reconcile S3 film objects with DB rows so viewers see bucket content."""

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import get_settings
from core.s3 import list_film_objects_by_id, presigned_stream_url
from core.tmdb import enrich_from_filename
from db.models import Film, FilmSource, FilmStatut

logger = logging.getLogger(__name__)


def sync_s3_films_to_db(db: Session) -> dict:
    """
    For each films/{id}/*.mp4 (etc.) in S3, ensure a Film row exists with statut disponible.
    Creates missing rows with explicit id matching the key prefix; updates s3_key when needed.
    """
    settings = get_settings()
    mapping = list_film_objects_by_id()
    bucket = settings.S3_BUCKET_NAME
    created = 0
    updated = 0

    for fid, key in sorted(mapping.items()):
        film = db.get(Film, fid)
        if film:
            changed = False
            if film.s3_key != key:
                film.s3_key = key
                changed = True
            if film.statut != FilmStatut.disponible:
                film.statut = FilmStatut.disponible
                changed = True
            if not film.s3_bucket:
                film.s3_bucket = bucket
                changed = True
            try:
                film.url_streaming = presigned_stream_url(key, expires=86400)
            except Exception as e:
                logger.warning("presign film_id=%s: %s", fid, e)
            if changed:
                updated += 1
        else:
            title = Path(key).stem.replace("_", " ")
            film = Film(
                id=fid,
                titre=title or f"Film {fid}",
                s3_key=key,
                s3_bucket=bucket,
                statut=FilmStatut.disponible,
                source=FilmSource.upload,
            )
            try:
                film.url_streaming = presigned_stream_url(key, expires=86400)
            except Exception as e:
                logger.warning("presign new film_id=%s: %s", fid, e)
            db.add(film)
            created += 1
            try:
                enrich = enrich_from_filename(Path(key).name)
                for k, v in enrich.items():
                    if hasattr(film, k) and v is not None:
                        setattr(film, k, v)
            except Exception as e:
                logger.debug("tmdb enrich film_id=%s: %s", fid, e)

    db.commit()

    if "postgresql" in settings.DATABASE_URL.lower():
        try:
            db.execute(
                text(
                    "SELECT setval(pg_get_serial_sequence('films', 'id'), "
                    "(SELECT COALESCE(MAX(id), 1) FROM films))"
                )
            )
            db.commit()
        except Exception as e:
            logger.warning("films id sequence sync skipped: %s", e)

    return {
        "created": created,
        "updated": updated,
        "keys_in_bucket": len(mapping),
    }
