"""Finalize a Film row after Vast remote transcode (S3 output -> library key). Comments in English."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from config import get_settings
from core.ffprobe import FFprobeError, probe, summarize
from core.logging_json import log_event
from core.s3 import (
    build_object_key,
    copy_object_key,
    delete_object_key,
    download_object_to_file,
    presigned_stream_url,
)
from core.tmdb import enrich_from_filename
from db.models import Film, FilmStatut, FilmTraitement
from db.session import SessionLocal

logger = logging.getLogger(__name__)


def mark_film_vast_task_failed(film_id: int, message: str) -> None:
    """
    Celery Vast transcode raised: mark film erreur, clear pipeline task fields.
    Keeps vast_pending_* so POST /transcode/vast/retry can re-queue the same S3 input.
    """
    db = SessionLocal()
    try:
        film = db.get(Film, film_id)
        if not film:
            return
        film.statut = FilmStatut.erreur
        film.erreur_message = (message or "Vast transcode failed")[:8000]
        film.pipeline_celery_task_id = None
        film.pipeline_celery_task_kind = None
        film.pipeline_progress = None
        db.commit()
        log_event(logger, "vast_transcode_error", film_id=film_id, error=message[:500])
    except Exception:
        logger.exception("mark_film_vast_task_failed film_id=%s", film_id)
    finally:
        db.close()


def _fail(db: Session, film: Film, message: str) -> None:
    from core.pipeline import _fail as pipeline_fail

    pipeline_fail(db, film, message)


def finalize_film_from_vast_s3_output(film_id: int, output_s3_key: str) -> None:
    """
    Copy vast-test transcoded MP4 into films/{id}/..., probe metadata, mark disponible,
    then delete the temporary vast-test output object.
    """
    db = SessionLocal()
    film: Film | None = None
    try:
        film = db.get(Film, film_id)
        if not film:
            logger.error("finalize_film_from_vast_s3_output: film %s not found", film_id)
            return

        dest_key = build_object_key(film_id, "video.mp4")
        copy_object_key(output_s3_key, dest_key)

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tmp_path = tf.name
        try:
            download_object_to_file(dest_key, tmp_path)
            data = probe(tmp_path)
            meta = summarize(data)
            sz_fallback = int(Path(tmp_path).stat().st_size)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        film.codec_video = meta.get("codec_video")
        film.codec_audio = meta.get("codec_audio")
        film.resolution = meta.get("resolution")
        film.bitrate_kbps = meta.get("bitrate_kbps")
        sz = int(meta.get("size_bytes") or 0)
        if sz <= 0:
            sz = sz_fallback
        film.taille_octets = sz if sz > 0 else None
        film.duree_min = meta.get("duration_min")
        film.traitement = FilmTraitement.transcode

        basename = (film.titre or "video").strip() or "video"
        enrich = enrich_from_filename(basename, film.content_kind)
        for k, v in enrich.items():
            if hasattr(film, k) and v is not None:
                setattr(film, k, v)

        s = get_settings()
        film.s3_key = dest_key
        film.s3_bucket = s.S3_BUCKET_NAME
        film.url_streaming = presigned_stream_url(dest_key, expires=86400)
        film.statut = FilmStatut.disponible
        film.erreur_message = None
        film.pipeline_progress = 100
        film.pipeline_celery_task_id = None
        film.pipeline_celery_task_kind = None
        film.vast_pending_job_token = None
        film.vast_pending_input_ext = None
        db.commit()
        logger.info("finalize_film_from_vast_s3_output: film_id=%s key=%s", film_id, dest_key)

        try:
            delete_object_key(output_s3_key)
        except Exception as e:
            logger.warning("finalize_film_from_vast_s3_output: could not delete %s: %s", output_s3_key, e)
    except FFprobeError as e:
        logger.exception("finalize_film_from_vast_s3_output probe film_id=%s", film_id)
        if film:
            _fail(db, film, str(e))
    except Exception as e:
        logger.exception("finalize_film_from_vast_s3_output film_id=%s", film_id)
        if film:
            _fail(db, film, str(e))
    finally:
        db.close()
