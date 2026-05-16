"""Admin-only routes: upload, queue, system, users, torrents."""

import base64
import calendar
import logging
import os
import re
from collections import defaultdict
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import String, case, cast, func, or_
from sqlalchemy.orm import Session

from api.deps import require_admin
from core.donation_settings_store import get_or_create_donation_settings
from api.routes.announcement import _get_or_create_row, _is_active
from api.routes.films import RefreshImdbApiBody, refresh_imdbapi as films_refresh_imdbapi
from config import get_settings
from core.admin_library_series import series_show_label_for_library_episode
from core.catalog_sync import sync_s3_films_to_db
from core.trailers_util import trailers_from_admin_lines, trailers_from_json_column, trailers_to_watch_urls
from core.gpu_detect import encoder_dict_for_api
from core.system_stats import collect_system_stats
from core.email_policy import validate_viewer_email
from core.donation_campaign import RECURRENCE_NONE, normalize_recurrence
from core.donation_service import compute_donation_snapshot
from core.member_invites import reset_member_invite_quota_current_month
from core.upload import save_upload_stream
from core.s3 import delete_film_prefix, delete_object_key, object_size_or_none, upload_file
from core.vast_transcode_cancel import cancel_vast_transcode_test, store_job_envelope
from db.models import (
    AuthPageAnnouncement,
    ContentKind,
    Film,
    FilmSource,
    FilmStatut,
    FilmTraitement,
    InvitationCode,
    SeriesSeasonMeta,
    SeriesShowMeta,
    User,
    UserRole,
    ViewerRank,
)
from db.session import get_db
from worker.tasks import download_torrent_task, process_film_task, vast_transcode_test_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _parse_torrent_transcode_target(raw: Optional[str]) -> str:
    v = (raw or "local").lower().strip()
    if v not in ("local", "vast"):
        raise HTTPException(400, "transcode_target doit être local ou vast")
    return v


def _parse_optional_positive_int_form(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    try:
        n = int(t)
    except ValueError:
        return None
    return n if n > 0 else None


def _viewer_rank_for_new_viewer(value: Optional[str]) -> str:
    if value is None or not str(value).strip():
        return ViewerRank.bronze.value
    v = str(value).strip().lower()
    try:
        ViewerRank(v)
    except ValueError:
        raise HTTPException(status_code=400, detail="Rang viewer invalide.")
    return v


def _viewer_rank_update_value(value: str) -> str:
    v = str(value).strip().lower()
    try:
        ViewerRank(v)
    except ValueError:
        raise HTTPException(status_code=400, detail="Rang viewer invalide.")
    return v


def _parse_upload_content_kind(raw: Optional[str]) -> ContentKind:
    """Multipart / form value: 'film' or 'series_episode'."""
    s = (raw or "film").strip()
    if s == ContentKind.film.value:
        return ContentKind.film
    if s == ContentKind.series_episode.value:
        return ContentKind.series_episode
    raise HTTPException(
        status_code=400,
        detail="content_kind doit être 'film' ou 'series_episode'",
    )


def _enqueue_process_film_or_raise(film_id: int, local_path: str, db: Session) -> None:
    """Queue Celery pipeline; on broker failure, remove temp file and mark film failed."""
    try:
        async_res = process_film_task.delay(film_id, local_path)
        row = db.get(Film, film_id)
        if row:
            row.pipeline_celery_task_id = async_res.id
            row.pipeline_celery_task_kind = "process_film"
            db.commit()
    except Exception:
        logger.exception(
            "enqueue process_film_task failed (Redis/Celery?). film_id=%s",
            film_id,
        )
        try:
            os.unlink(local_path)
        except OSError:
            pass
        row = db.get(Film, film_id)
        if row:
            row.statut = FilmStatut.erreur
            row.erreur_message = (
                "File d'attente indisponible : impossible de joindre Redis/Celery. "
                "Vérifiez REDIS_PASSWORD, REDIS_HOST et que les conteneurs redis et worker sont actifs."
            )[:8000]
            db.commit()
        raise HTTPException(
            status_code=503,
            detail=(
                "Le traitement vidéo n'a pas pu être mis en file d'attente (Redis). "
                "Vérifiez docker compose (services redis, worker), REDIS_PASSWORD et REDIS_HOST dans docker/.env."
            ),
        )


def _enqueue_download_torrent_or_raise(film_id: int, db: Session) -> None:
    try:
        async_res = download_torrent_task.delay(film_id)
        row = db.get(Film, film_id)
        if row:
            row.pipeline_celery_task_id = async_res.id
            row.pipeline_celery_task_kind = "download_torrent"
            db.commit()
    except Exception:
        logger.exception(
            "enqueue download_torrent_task failed (Redis/Celery?). film_id=%s",
            film_id,
        )
        row = db.get(Film, film_id)
        if row:
            row.statut = FilmStatut.erreur
            row.erreur_message = (
                "File d'attente indisponible : impossible de joindre Redis/Celery."
            )[:8000]
            db.commit()
        raise HTTPException(
            status_code=503,
            detail=(
                "La file d'attente (Redis) est injoignable. "
                "Vérifiez les conteneurs redis et worker, REDIS_PASSWORD et REDIS_HOST."
            ),
        )


def _apply_admin_library_q(query, q: Optional[str]):
    if q and str(q).strip():
        like = f"%{str(q).strip()}%"
        return query.filter(
            or_(
                Film.titre.ilike(like),
                Film.realisateur.ilike(like),
                Film.series_title.ilike(like),
                Film.series_key.ilike(like),
            )
        )
    return query


def _admin_film_row_dict(f: Film) -> dict[str, Any]:
    return {
        "id": f.id,
        "titre": f.titre,
        "titre_original": f.titre_original,
        "realisateur": f.realisateur,
        "annee": f.annee,
        "taille_octets": f.taille_octets,
        "codec_video": f.codec_video,
        "traitement": f.traitement.value if f.traitement else None,
        "statut": f.statut.value,
        "poster_path": f.poster_path,
        "source": f.source.value,
        "erreur_message": f.erreur_message,
        "content_kind": f.content_kind.value,
        "series_title": f.series_title,
        "series_key": f.series_key,
        "season_number": f.season_number,
        "episode_number": f.episode_number,
        "s3_key": f.s3_key,
    }


@router.get("/library-meta")
def admin_library_meta(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    q: Optional[str] = None,
):
    """Totals for the admin library header (same search filter as list endpoints)."""
    base = _apply_admin_library_q(db.query(Film), q)
    films_total = base.filter(Film.content_kind == ContentKind.film).count()
    ep_q = base.filter(Film.content_kind == ContentKind.series_episode)
    thin = ep_q.with_entities(Film.series_title, Film.series_key, Film.titre).all()
    episodes_total = len(thin)
    series_labels = {
        series_show_label_for_library_episode(st, sk, tit) for st, sk, tit in thin
    }
    series_shows_total = len(series_labels)
    return {
        "films_total": films_total,
        "episodes_total": episodes_total,
        "series_shows_total": series_shows_total,
    }


@router.get("/films")
def admin_list_films(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    q: Optional[str] = None,
    content_kind: str = Query(
        ...,
        description="film — longs métrages; series_episode — épisodes regroupés côté UI.",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    paginate_by: str = Query(
        "episode",
        description="series_episode only: episode — 10 rows = 10 épisodes; "
        "series_show — 10 rows = 10 blocs série (tous épisodes de ces séries).",
    ),
):
    if content_kind not in ("film", "series_episode"):
        raise HTTPException(400, "content_kind must be 'film' or 'series_episode'")
    if paginate_by not in ("episode", "series_show"):
        raise HTTPException(400, "paginate_by must be 'episode' or 'series_show'")
    if content_kind == "film" and paginate_by != "episode":
        raise HTTPException(400, "paginate_by is only valid for content_kind=series_episode")

    kind = ContentKind.film if content_kind == "film" else ContentKind.series_episode
    query = (
        _apply_admin_library_q(db.query(Film), q)
        .filter(Film.content_kind == kind)
        .order_by(Film.date_ajout.desc())
    )

    if kind == ContentKind.series_episode and paginate_by == "series_show":
        thin = query.with_entities(
            Film.id,
            Film.series_title,
            Film.series_key,
            Film.titre,
        ).all()
        label_to_ids: dict[str, list[int]] = defaultdict(list)
        for fid, st, sk, tit in thin:
            lab = series_show_label_for_library_episode(st, sk, tit)
            label_to_ids[lab].append(fid)
        sorted_labels = sorted(label_to_ids.keys(), key=str.casefold)
        total_shows = len(sorted_labels)
        offset = (page - 1) * page_size
        page_labels = sorted_labels[offset : offset + page_size]
        ordered_ids: list[int] = []
        for lab in page_labels:
            ordered_ids.extend(label_to_ids[lab])
        if not ordered_ids:
            return {
                "items": [],
                "total": total_shows,
                "page": page,
                "page_size": page_size,
                "paginate_by": paginate_by,
            }
        by_id = {
            f.id: f
            for f in db.query(Film)
            .filter(Film.id.in_(ordered_ids))
            .order_by(Film.date_ajout.desc())
            .all()
        }
        rows = [by_id[i] for i in ordered_ids if i in by_id]
        return {
            "items": [_admin_film_row_dict(f) for f in rows],
            "total": total_shows,
            "page": page,
            "page_size": page_size,
            "paginate_by": paginate_by,
        }

    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [_admin_film_row_dict(f) for f in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "paginate_by": "episode",
    }


def _film_to_admin_detail(f: Film) -> dict[str, Any]:
    genres = f.genres if isinstance(f.genres, list) else []
    acteurs = f.acteurs if isinstance(f.acteurs, list) else []
    return {
        "id": f.id,
        "titre": f.titre,
        "titre_original": f.titre_original,
        "annee": f.annee,
        "synopsis": f.synopsis,
        "genres": [str(x) for x in genres],
        "realisateur": f.realisateur,
        "acteurs": [str(x) for x in acteurs],
        "note_tmdb": f.note_tmdb,
        "poster_path": f.poster_path,
        "duree_min": f.duree_min,
        "resolution": f.resolution,
        "langue_originale": f.langue_originale,
        "tmdb_id": f.tmdb_id,
        "imdb_title_id": f.imdb_title_id,
        "statut": f.statut.value,
        "codec_video": f.codec_video,
        "taille_octets": f.taille_octets,
        "content_kind": f.content_kind.value,
        "series_key": f.series_key,
        "series_title": f.series_title,
        "season_number": f.season_number,
        "episode_number": f.episode_number,
        "trailers_manual_lines": trailers_to_watch_urls(trailers_from_json_column(f.trailers_manual)),
    }


def _estimated_gpu_rental_usd(duree_min: Optional[int], dph_usd: float) -> Optional[float]:
    """Rough Vast-style rental hint: title duration (hours) × max $/h from settings."""
    if duree_min is None or int(duree_min) <= 0:
        return None
    hours = float(duree_min) / 60.0
    return round(hours * float(dph_usd), 4)


@router.get("/films/{film_id}/processing-state")
def admin_film_processing_state(
    film_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Live pipeline fields for admin « Sur la machine » UI (progress, stream URL, cost hint).
    """
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Film not found")
    s = get_settings()
    dph = float(s.VAST_MAX_DPH_PER_HOUR)
    pct = int(f.pipeline_progress or 0)
    if f.statut == FilmStatut.en_cours and pct <= 0:
        pct = 1
    return {
        "id": f.id,
        "titre": f.titre,
        "statut": f.statut.value,
        "traitement": f.traitement.value if f.traitement else None,
        "pipeline_progress": pct,
        "duree_min": f.duree_min,
        "codec_video": f.codec_video,
        "codec_audio": f.codec_audio,
        "resolution": f.resolution,
        "taille_octets": f.taille_octets,
        "bitrate_kbps": f.bitrate_kbps,
        "url_streaming": f.url_streaming if f.statut == FilmStatut.disponible else None,
        "erreur_message": f.erreur_message if f.statut == FilmStatut.erreur else None,
        "estimated_gpu_rental_usd": _estimated_gpu_rental_usd(f.duree_min, dph),
        "pricing_dph_usd": dph,
        "pricing_note_fr": (
            "Estimation indicative : durée du titre × VAST_MAX_DPH_PER_HOUR "
            "(location GPU type Vast). Ce n’est pas une facture ; le transcodage "
            "réel tourne sur votre worker local."
        ),
    }


class AdminBulkDeleteFilmsBody(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=100)


@router.post("/films/bulk-delete")
def admin_bulk_delete_films(
    body: AdminBulkDeleteFilmsBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Delete multiple catalog entries (films or episodes) in one request."""
    unique_ids = list(dict.fromkeys(body.ids))
    deleted: List[int] = []
    errors: List[Dict[str, Any]] = []
    for film_id in unique_ids:
        f = db.get(Film, film_id)
        if not f:
            errors.append({"id": film_id, "error": "not_found"})
            continue
        try:
            delete_film_prefix(film_id, known_s3_key=f.s3_key)
        except Exception as e:
            logger.exception("s3 delete failed for film_id=%s", film_id)
            errors.append({"id": film_id, "error": str(e)})
            continue
        db.delete(f)
        deleted.append(film_id)
    if deleted:
        db.flush()
        db.commit()
    return {"deleted": deleted, "errors": errors}


@router.get("/films/{film_id}")
def admin_get_film(
    film_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Film not found")
    return _film_to_admin_detail(f)


class AdminFilmUpdateBody(BaseModel):
    titre: str = Field(..., min_length=1, max_length=512)
    titre_original: Optional[str] = None
    annee: Optional[int] = None
    synopsis: Optional[str] = None
    genres: List[str] = []
    realisateur: Optional[str] = None
    acteurs: List[str] = []
    note_tmdb: Optional[float] = None
    poster_path: Optional[str] = None
    duree_min: Optional[int] = None
    resolution: Optional[str] = None
    langue_originale: Optional[str] = None
    tmdb_id: Optional[int] = None
    imdb_title_id: Optional[str] = None
    content_kind: ContentKind = ContentKind.film
    series_key: Optional[str] = None
    series_title: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    # One line per trailer: YouTube URL or 11-char key; optional "Title|url"
    trailers_manual: Optional[List[str]] = None


@router.patch("/films/{film_id}")
def admin_patch_film(
    film_id: int,
    body: AdminFilmUpdateBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Film not found")
    f.titre = body.titre.strip()
    f.titre_original = (body.titre_original or "").strip() or None
    f.annee = body.annee
    syn = (body.synopsis or "").strip()
    f.synopsis = syn or None
    f.genres = [g.strip() for g in body.genres if g and str(g).strip()] or None
    f.realisateur = (body.realisateur or "").strip() or None
    f.acteurs = [a.strip() for a in body.acteurs if a and str(a).strip()] or None
    f.note_tmdb = body.note_tmdb
    pp = (body.poster_path or "").strip()
    f.poster_path = pp or None
    f.duree_min = body.duree_min
    f.resolution = (body.resolution or "").strip() or None
    lo = (body.langue_originale or "").strip()
    f.langue_originale = lo or None
    prev_tmdb_id = f.tmdb_id
    f.tmdb_id = body.tmdb_id
    if prev_tmdb_id != body.tmdb_id:
        f.trailers_tmdb_cache = None
        f.trailers_tmdb_cached_at = None
    if "imdb_title_id" in body.model_fields_set:
        imdb_s = (body.imdb_title_id or "").strip()
        if imdb_s and not re.fullmatch(r"tt\d+", imdb_s, re.IGNORECASE):
            raise HTTPException(
                status_code=400,
                detail="ID IMDb invalide (format attendu : tt1234567)",
            )
        f.imdb_title_id = imdb_s or None
    f.content_kind = body.content_kind
    if body.content_kind == ContentKind.film:
        f.series_key = None
        f.series_title = None
        f.season_number = None
        f.episode_number = None
    else:
        f.series_key = (body.series_key or "").strip() or None
        f.series_title = (body.series_title or "").strip() or None
        ssn = body.season_number
        een = body.episode_number
        if ssn is None or een is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Pour un épisode de série, la saison et le numéro d'épisode sont "
                    "obligatoires (vérifiez qu'ils sont bien envoyés par l'interface admin)."
                ),
            )
        if int(ssn) < 0 or int(een) < 1:
            raise HTTPException(
                status_code=400,
                detail="Saison ≥ 0 et épisode ≥ 1 requis pour un épisode de série.",
            )
        f.season_number = int(ssn)
        f.episode_number = int(een)
    if body.trailers_manual is not None:
        parsed = trailers_from_admin_lines(body.trailers_manual)
        f.trailers_manual = parsed if parsed else None
    db.commit()
    db.refresh(f)
    return _film_to_admin_detail(f)


@router.post("/films/{film_id}/refresh-tmdb")
def admin_refresh_tmdb(
    film_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Same as POST /api/films/{id}/refresh-tmdb; exposed under /api/admin for the admin UI."""
    from api.routes.films import refresh_tmdb as films_refresh_tmdb

    return films_refresh_tmdb(film_id, db, _)


@router.post("/films/{film_id}/refresh-imdbapi")
def admin_refresh_imdbapi(
    film_id: int,
    body: RefreshImdbApiBody = Body(default_factory=RefreshImdbApiBody),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Same as POST /api/films/{id}/refresh-imdbapi; exposed under /api/admin for the admin UI."""
    return films_refresh_imdbapi(film_id, body, db, _)


@router.post("/upload")
async def admin_upload(
    file: UploadFile = File(...),
    content_kind: str = Form("film"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        path, size = await save_upload_stream(file)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        logger.exception("upload: failed to write file under /tmp/redwood/uploads")
        raise HTTPException(
            status_code=507,
            detail=(
                "Écriture du fichier impossible (disque plein ou répertoire non inscriptible). "
                "En production Docker : le volume tmp_data monté sur /tmp/redwood doit être "
                "accessible en écriture par l’utilisateur redwood (uid 1000). Reconstruire les "
                "images et redémarrer les services api/worker après mise à jour. "
                f"Détail : {e}"
            ),
        ) from e

    ck = _parse_upload_content_kind(content_kind)
    film = Film(
        titre=Path(file.filename or "upload").stem,
        source=FilmSource.upload,
        statut=FilmStatut.en_cours,
        pipeline_progress=0,
        content_kind=ck,
        pipeline_staging_path=path,
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    _enqueue_process_film_or_raise(film.id, path, db)
    return {"job_id": film.id, "filename": file.filename, "size_bytes": size}


class TorrentMagnetBody(BaseModel):
    magnet: str
    content_kind: ContentKind = ContentKind.film
    transcode_target: str = "local"
    vast_offer_id: Optional[int] = None

    @field_validator("transcode_target")
    @classmethod
    def _validate_transcode_target(cls, v: str) -> str:
        s = (v or "local").lower().strip()
        if s not in ("local", "vast"):
            raise ValueError("transcode_target must be local or vast")
        return s


@router.post("/torrents")
def admin_torrent_magnet(
    body: TorrentMagnetBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if not body.magnet.startswith("magnet:?"):
        raise HTTPException(400, "Invalid magnet link")
    s = get_settings()
    if body.transcode_target == "vast" and not (s.VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY requise pour enchaîner torrent → transcodage Vast.",
        )
    vast_oid = body.vast_offer_id if body.vast_offer_id and body.vast_offer_id > 0 else None
    film = Film(
        titre="Torrent",
        source=FilmSource.torrent,
        statut=FilmStatut.en_cours,
        pipeline_progress=0,
        content_kind=body.content_kind,
        transcode_target=body.transcode_target,
        vast_offer_id=vast_oid if body.transcode_target == "vast" else None,
        torrent_magnet_uri=body.magnet.strip(),
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    _enqueue_download_torrent_or_raise(film.id, db)
    return {"job_id": film.id}


@router.post("/torrents/file")
async def admin_torrent_file(
    torrent: UploadFile = File(...),
    content_kind: str = Form("film"),
    transcode_target: str = Form("local"),
    vast_offer_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if not torrent.filename or not torrent.filename.lower().endswith(".torrent"):
        raise HTTPException(400, "Expected .torrent file")
    tt = _parse_torrent_transcode_target(transcode_target)
    s = get_settings()
    if tt == "vast" and not (s.VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY requise pour enchaîner torrent → transcodage Vast.",
        )
    data = await torrent.read()
    ck = _parse_upload_content_kind(content_kind)
    v_oid = _parse_optional_positive_int_form(vast_offer_id) if tt == "vast" else None
    film = Film(
        titre=Path(torrent.filename).stem,
        source=FilmSource.torrent,
        statut=FilmStatut.en_cours,
        pipeline_progress=0,
        content_kind=ck,
        transcode_target=tt,
        vast_offer_id=v_oid,
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    from core.torrent_blobs import ensure_torrent_blobs_dir, torrent_blob_path_for_film_id

    ensure_torrent_blobs_dir()
    blob_disk = torrent_blob_path_for_film_id(film.id)
    blob_disk.write_bytes(data)
    film.torrent_blob_path = str(blob_disk)
    db.commit()
    _enqueue_download_torrent_or_raise(film.id, db)
    return {"job_id": film.id}


@router.post("/catalog/sync-s3")
def admin_sync_s3(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Import or update Film rows from objects already stored under films/{id}/ in S3."""
    try:
        return sync_s3_films_to_db(db)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e


class CreateInviteBody(BaseModel):
    max_uses: int = 1
    note: Optional[str] = None
    code: Optional[str] = None
    expires_days: Optional[int] = None


class InviteListItemOut(BaseModel):
    id: int
    code: str
    max_uses: int
    uses: int
    note: Optional[str] = None
    expires_at: Optional[str] = None
    created_at: Optional[str] = None


class PaginatedInvitesOut(BaseModel):
    items: List[InviteListItemOut]
    total: int
    total_all: int
    page: int
    page_size: int


@router.get("/invites", response_model=PaginatedInvitesOut)
def list_invites(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    qn = (q or "").strip().lower()

    def _filtered_invites_query():
        bq = db.query(InvitationCode)
        if not qn:
            return bq
        like = f"%{qn}%"
        uses_pair = func.lower(
            func.concat(
                cast(InvitationCode.uses, String),
                "/",
                cast(InvitationCode.max_uses, String),
            )
        )
        exp_str = func.lower(func.coalesce(cast(InvitationCode.expires_at, String), ""))
        return bq.filter(
            or_(
                func.lower(InvitationCode.code).like(like),
                func.lower(func.coalesce(InvitationCode.note, "")).like(like),
                uses_pair.like(like),
                exp_str.like(like),
            )
        )

    filtered_q = _filtered_invites_query()
    filtered_total = int(filtered_q.count())
    total_all = int(db.query(func.count(InvitationCode.id)).scalar() or 0)
    total_pages = max(1, (filtered_total + page_size - 1) // page_size)
    safe_page = min(max(1, page), total_pages)
    rows = (
        _filtered_invites_query()
        .order_by(InvitationCode.created_at.desc())
        .offset((safe_page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        InviteListItemOut(
            id=r.id,
            code=r.code,
            max_uses=r.max_uses,
            uses=r.uses,
            note=r.note,
            expires_at=r.expires_at.isoformat() if r.expires_at else None,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]
    return PaginatedInvitesOut(
        items=items,
        total=filtered_total,
        total_all=total_all,
        page=safe_page,
        page_size=page_size,
    )


@router.post("/invites")
def create_invite(
    body: CreateInviteBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    raw = (body.code or "").strip().upper() or secrets.token_hex(5).upper()
    if db.query(InvitationCode).filter(InvitationCode.code == raw).first():
        raise HTTPException(400, "Code already exists")
    exp = None
    if body.expires_days and body.expires_days > 0:
        exp = datetime.utcnow() + timedelta(days=body.expires_days)
    inv = InvitationCode(
        code=raw,
        max_uses=max(1, body.max_uses),
        note=(body.note or "")[:255] or None,
        expires_at=exp,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return {
        "id": inv.id,
        "code": inv.code,
        "max_uses": inv.max_uses,
        "uses": inv.uses,
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
    }


@router.delete("/invites/{invite_id}", status_code=204)
def delete_invite(
    invite_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    inv = db.get(InvitationCode, invite_id)
    if not inv:
        raise HTTPException(404, "Not found")
    db.delete(inv)
    db.commit()


def _film_retry_local_upload_available(f: Film) -> bool:
    if f.statut != FilmStatut.erreur or f.source != FilmSource.upload:
        return False
    tgt = (getattr(f, "transcode_target", None) or "local").lower().strip()
    if tgt != "local":
        return False
    p = (getattr(f, "pipeline_staging_path", None) or "").strip()
    return bool(p and os.path.isfile(p))


def _vast_instance_id_from_celery_task_id(task_id: Optional[str]) -> Optional[int]:
    """Vast contract id from Celery result meta (worker.tasks.vast_transcode_test_task PROGRESS)."""
    tid = (task_id or "").strip()
    if not tid:
        return None
    try:
        from celery.result import AsyncResult

        from worker.tasks import app as celery_app

        res = AsyncResult(tid, app=celery_app)
        info = res.info
        if not isinstance(info, dict):
            return None
        raw = info.get("vast_instance_id")
        if raw is None:
            return None
        return int(raw)
    except Exception:
        logger.debug("vast_instance_id lookup failed for task_id=%s", tid, exc_info=True)
        return None


@router.get("/queue")
def admin_queue(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    items = (
        db.query(Film)
        .filter(
            or_(
                Film.statut == FilmStatut.en_cours,
                Film.statut == FilmStatut.erreur,
            )
        )
        .order_by(Film.date_ajout.desc())
        .limit(100)
        .all()
    )
    out: List[dict[str, Any]] = []
    for f in items:
        label = f.titre
        sub = f.traitement.value if f.traitement else "Analyse…"
        if f.statut == FilmStatut.erreur:
            sub = f.erreur_message or "Erreur"
        pct = f.pipeline_progress or (50 if f.statut == FilmStatut.en_cours else 0)
        tid = getattr(f, "pipeline_celery_task_id", None)
        tkind = (getattr(f, "pipeline_celery_task_kind", None) or "").strip()
        vast_inst = (
            _vast_instance_id_from_celery_task_id(str(tid).strip() if tid else "")
            if tkind == "vast_transcode" and tid
            else None
        )
        out.append(
            {
                "id": f.id,
                "filename": label,
                "statut": f.statut.value,
                "traitement": f.traitement.value if f.traitement else None,
                "progress": pct,
                "erreur_message": f.erreur_message,
                "source": f.source.value,
                "torrent_stats": f.torrent_stats,
                "transcode_target": getattr(f, "transcode_target", None) or "local",
                "celery_task_id": tid,
                "celery_task_kind": getattr(f, "pipeline_celery_task_kind", None),
                "vast_instance_id": vast_inst,
                "vast_retryable": _film_vast_retryable(f),
                "retry_local_available": _film_retry_local_upload_available(f),
            }
        )
    return {"items": out}


@router.post("/queue/jobs/{film_id}/retry-local")
def admin_retry_local_pipeline_job(
    film_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Re-enqueue process_film for a failed/cancelled direct upload when the staging file still exists."""
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Film introuvable")
    if f.statut != FilmStatut.erreur:
        raise HTTPException(
            status_code=409,
            detail="Seuls les films en erreur peuvent être relancés depuis la file.",
        )
    if f.source != FilmSource.upload:
        raise HTTPException(
            status_code=409,
            detail="La relance locale n'est disponible que pour les uploads directs.",
        )
    tgt = (getattr(f, "transcode_target", None) or "local").lower().strip()
    if tgt != "local":
        raise HTTPException(
            status_code=409,
            detail="Relance impossible : ce titre n'est pas configuré pour le pipeline local.",
        )
    local_path = (getattr(f, "pipeline_staging_path", None) or "").strip()
    if not local_path or not os.path.isfile(local_path):
        raise HTTPException(
            status_code=409,
            detail=(
                "Fichier source introuvable sur le disque (supprimé ou non enregistré). "
                "Ré-uploadez le fichier."
            ),
        )
    f.statut = FilmStatut.en_cours
    f.erreur_message = None
    f.pipeline_progress = 0
    f.pipeline_celery_task_id = None
    f.pipeline_celery_task_kind = None
    f.torrent_stats = None
    f.traitement = None
    db.commit()
    _enqueue_process_film_or_raise(film_id, local_path, db)
    return {"ok": True, "film_id": film_id}


@router.post("/queue/jobs/{film_id}/cancel")
def admin_cancel_pipeline_job(
    film_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Revoke the worker's active Celery task for this film (torrent download, local encode, or Vast transcode)."""
    from worker.tasks import app as celery_app

    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Film introuvable")
    if f.statut != FilmStatut.en_cours:
        raise HTTPException(
            status_code=409,
            detail="Seuls les films en cours peuvent être annulés depuis la file.",
        )
    tid = (getattr(f, "pipeline_celery_task_id", None) or "").strip()
    kind = (getattr(f, "pipeline_celery_task_kind", None) or "").strip()
    if not tid:
        raise HTTPException(
            status_code=409,
            detail="Aucune tâche Celery n’est encore enregistrée pour ce film. Réessayez après rafraîchissement.",
        )
    try:
        if kind == "vast_transcode":
            s = get_settings()
            if not (s.VAST_API_KEY or "").strip():
                raise HTTPException(
                    status_code=503,
                    detail="VAST_API_KEY requise pour annuler le transcodage Vast.",
                )
            cancel_vast_transcode_test(celery_app, tid)
        else:
            from worker.tasks import _revoke_celery_task_or_group

            _revoke_celery_task_or_group(
                celery_app,
                tid,
                is_group=(kind == "process_series_pack"),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_cancel_pipeline_job film_id=%s", film_id)
        raise HTTPException(status_code=502, detail=str(e)[:2000]) from e

    f.statut = FilmStatut.erreur
    f.erreur_message = "Annulé par l’administrateur."[:8000]
    f.pipeline_celery_task_id = None
    f.pipeline_celery_task_kind = None
    f.torrent_stats = None
    f.pipeline_progress = None
    db.commit()
    return {"ok": True, "film_id": film_id, "revoked_celery_task_id": tid}


def _month_bucket_expr(column, dialect_name: str):
    if dialect_name == "postgresql":
        return func.date_trunc("month", column)
    return func.strftime("%Y-%m", column)


def _add_calendar_months(y: int, mo: int, delta: int) -> Tuple[int, int]:
    """Add ``delta`` calendar months to ``(y, mo)``; ``mo`` is 1..12."""
    total = y * 12 + (mo - 1) + delta
    y2 = total // 12
    mo2 = total % 12 + 1
    return y2, mo2


def _utc_month_label_series(num_months: int) -> List[str]:
    """
    Last ``num_months`` calendar months in UTC (oldest first).
    Matches naive UTC timestamps used for Film.date_ajout / User.date_creation.
    """
    now = datetime.now(timezone.utc)
    y, mo = now.year, now.month
    y0, m0 = _add_calendar_months(y, mo, -(num_months - 1))
    out: List[str] = []
    cy, cm = y0, m0
    for _ in range(num_months):
        out.append(f"{cy:04d}-{cm:02d}")
        cy, cm = _add_calendar_months(cy, cm, 1)
    return out


def _dense_series_for_month_labels(
    labels: List[str], sparse: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """One entry per label month; counts default to 0 (Chart.js / admin UI)."""
    smap: Dict[str, int] = {}
    for x in sparse or []:
        key = str(x.get("month") or "")[:7]
        if len(key) == 7 and key[4] == "-":
            smap[key] = int(x.get("count") or 0)
    return [{"month": lab, "count": int(smap.get(lab, 0))} for lab in labels]


def _day_bucket_expr(column, dialect_name: str):
    if dialect_name == "postgresql":
        return func.date_trunc("day", column)
    return func.strftime("%Y-%m-%d", column)


def _monthly_counts(db: Session, column, since: datetime) -> List[Dict[str, Any]]:
    dialect = db.get_bind().dialect.name
    bucket = _month_bucket_expr(column, dialect)
    rows = (
        db.query(bucket, func.count(1))
        .filter(column.isnot(None), column >= since)
        .group_by(bucket)
        .order_by(bucket)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for b, cnt in rows:
        if b is None:
            continue
        if dialect == "postgresql":
            try:
                label = b.date().isoformat()[:7]  # type: ignore[union-attr]
            except Exception:
                label = str(b)[:7]
        else:
            label = str(b)[:7]
        out.append({"month": label, "count": int(cnt)})
    return out


@router.get("/statistics/overview")
def admin_statistics_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    months: int = Query(12, ge=1, le=36, description="Months of history for time series."),
):
    """
    Aggregated metrics for the admin dashboard: users, films, storage (catalog taille_octets),
    Vast-related counts and a rough spend upper bound from settings (not live Vast billing).
    """
    s = get_settings()

    total_users = int(db.query(func.count(User.id)).scalar() or 0)
    active_users = int(
        db.query(func.count(User.id)).filter(User.is_active.is_(True)).scalar() or 0
    )
    admins_count = int(db.query(func.count(User.id)).filter(User.role == UserRole.admin).scalar() or 0)
    viewers_count = int(db.query(func.count(User.id)).filter(User.role == UserRole.viewer).scalar() or 0)

    total_films = int(db.query(func.count(Film.id)).scalar() or 0)
    ok = int(db.query(func.count(Film.id)).filter(Film.statut == FilmStatut.disponible).scalar() or 0)
    err = int(db.query(func.count(Film.id)).filter(Film.statut == FilmStatut.erreur).scalar() or 0)
    pending = int(db.query(func.count(Film.id)).filter(Film.statut == FilmStatut.en_cours).scalar() or 0)

    films_feature = int(
        db.query(func.count(Film.id)).filter(Film.content_kind == ContentKind.film).scalar() or 0
    )
    episodes = int(
        db.query(func.count(Film.id)).filter(Film.content_kind == ContentKind.series_episode).scalar() or 0
    )

    by_source: dict[str, int] = {}
    for src, n in db.query(Film.source, func.count(Film.id)).group_by(Film.source).all():
        key = getattr(src, "value", str(src))
        by_source[key] = int(n)

    trait_label = case(
        (Film.traitement == FilmTraitement.direct, "direct"),
        (Film.traitement == FilmTraitement.optimise, "optimise"),
        (Film.traitement == FilmTraitement.transcode, "transcode"),
        else_="unknown",
    )
    by_traitement: dict[str, int] = {}
    for lab, n in db.query(trait_label, func.count(Film.id)).group_by(trait_label).all():
        by_traitement[str(lab)] = int(n)

    sum_bytes_row = (
        db.query(func.coalesce(func.sum(Film.taille_octets), 0))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.taille_octets.isnot(None),
            Film.taille_octets > 0,
        )
        .scalar()
    )
    total_bytes = int(sum_bytes_row or 0)
    sized_count = int(
        db.query(func.count(Film.id))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.taille_octets.isnot(None),
            Film.taille_octets > 0,
        )
        .scalar()
        or 0
    )
    total_gb = round(total_bytes / (1024.0**3), 3) if total_bytes else 0.0

    vast_target = int(
        db.query(func.count(Film.id))
        .filter(Film.transcode_target == "vast")
        .scalar()
        or 0
    )
    vast_done = int(
        db.query(func.count(Film.id))
        .filter(Film.transcode_target == "vast", Film.statut == FilmStatut.disponible)
        .scalar()
        or 0
    )
    dph_cap = float(s.VAST_MAX_DPH_PER_HOUR)
    minutes_sum_row = (
        db.query(func.coalesce(func.sum(Film.duree_min), 0))
        .filter(Film.transcode_target == "vast", Film.statut == FilmStatut.disponible)
        .scalar()
    )
    total_minutes_vast_done = int(minutes_sum_row or 0)
    est_hours = total_minutes_vast_done / 60.0
    est_usd_upper = round(est_hours * dph_cap, 4) if vast_done else 0.0

    invites_total = int(db.query(func.count(InvitationCode.id)).scalar() or 0)
    invites_uses = int(db.query(func.coalesce(func.sum(InvitationCode.uses), 0)).scalar() or 0)

    month_labels = _utc_month_label_series(months)
    y0, m0 = int(month_labels[0][:4]), int(month_labels[0][5:7])
    since = datetime(y0, m0, 1, 0, 0, 0)
    films_by_month_sparse = _monthly_counts(db, Film.date_ajout, since)
    users_by_month_sparse = _monthly_counts(db, User.date_creation, since)
    films_by_month = _dense_series_for_month_labels(month_labels, films_by_month_sparse)
    users_by_month = _dense_series_for_month_labels(month_labels, users_by_month_sparse)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "users": {
            "total": total_users,
            "active": active_users,
            "inactive": max(0, total_users - active_users),
            "admins": admins_count,
            "viewers": viewers_count,
        },
        "films": {
            "total": total_films,
            "disponible": ok,
            "en_cours": pending,
            "erreur": err,
            "feature_films": films_feature,
            "series_episodes": episodes,
            "by_source": by_source,
            "by_traitement": by_traitement,
        },
        "storage": {
            "total_bytes": total_bytes,
            "total_gb": total_gb,
            "disponible_with_size_count": sized_count,
            "note": "Somme des taille_octets des contenus disponibles (métadonnées catalogue, pas un audit S3 byte-par-byte).",
        },
        "vast": {
            "films_transcode_target_vast": vast_target,
            "films_vast_disponible": vast_done,
            "estimated_gpu_hours_from_film_duration": round(est_hours, 2),
            "estimated_rental_usd_upper_bound": est_usd_upper,
            "pricing_dph_usd_assumption": dph_cap,
            "disclaimer": (
                "Estimation indicative : durée des titres (duree_min) × VAST_MAX_DPH_PER_HOUR. "
                "La facturation réelle Vast.ai dépend des offres, du temps réel de VM et du trafic."
            ),
        },
        "invites": {"codes_total": invites_total, "uses_total": invites_uses},
        "timeseries": {
            "months": months,
            "month_labels": month_labels,
            "films_added_by_month": films_by_month,
            "users_registered_by_month": users_by_month,
        },
    }


def _billing_month_label(bucket_val: Any, dialect: str) -> str:
    if bucket_val is None:
        return ""
    if dialect == "postgresql":
        try:
            return bucket_val.date().isoformat()[:7]
        except Exception:
            return str(bucket_val)[:7]
    return str(bucket_val)[:7]


def _billing_day_label(bucket_val: Any, dialect: str) -> str:
    if bucket_val is None:
        return ""
    if dialect == "postgresql":
        try:
            return bucket_val.date().isoformat()[:10]
        except Exception:
            return str(bucket_val)[:10]
    return str(bucket_val)[:10]


def _iter_month_labels(from_month: str, to_month: str) -> List[str]:
    """from_month / to_month as YYYY-MM inclusive."""
    y1, m1 = int(from_month[:4]), int(from_month[5:7])
    y2, m2 = int(to_month[:4]), int(to_month[5:7])
    out: List[str] = []
    y, m = y1, m1
    while (y, m) <= (y2, m2):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _iter_day_labels(from_day: str, to_day: str) -> List[str]:
    """from_day / to_day as YYYY-MM-DD inclusive."""
    d = datetime.strptime(from_day[:10], "%Y-%m-%d").date()
    end = datetime.strptime(to_day[:10], "%Y-%m-%d").date()
    out: List[str] = []
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _catalog_bytes_before(db: Session, before: datetime) -> int:
    """Sum taille_octets for disponible titles added before ``before`` (or without date_ajout)."""
    row = (
        db.query(func.coalesce(func.sum(Film.taille_octets), 0))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.taille_octets.isnot(None),
            Film.taille_octets > 0,
            or_(Film.date_ajout.is_(None), Film.date_ajout < before),
        )
        .scalar()
    )
    return int(row or 0)


@router.get("/billing/overview")
def admin_billing_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    months: int = Query(18, ge=3, le=60, description="Months of monthly storage / Vast series."),
    vast_daily_days: int = Query(90, ge=7, le=120, description="Days for Vast est. daily chart."),
    storage_daily_days: int = Query(90, ge=7, le=365, description="Days for storage daily charts."),
):
    """
    Billing-oriented estimates: storage (€/GiB/h HT), Vast transcode upper bound (USD→EUR),
    per-film hints, monthly/daily series from DB (no S3 byte audit).
    """
    s = get_settings()
    dialect = db.get_bind().dialect.name
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    since = now - timedelta(days=32 * months)
    since_day = now - timedelta(days=int(vast_daily_days))
    since_storage_day = now - timedelta(days=int(storage_daily_days))

    rate_gib_h = float(getattr(s, "BILLING_STORAGE_EUR_PER_GIB_HOUR_HT", 0.0) or 0.0)
    usd_to_eur = float(getattr(s, "BILLING_USD_TO_EUR", 0.92) or 0.92)
    dph_usd = float(s.VAST_MAX_DPH_PER_HOUR)
    local_eur_per_min = float(getattr(s, "BILLING_LOCAL_TRANSCODE_EUR_PER_MINUTE", 0.0) or 0.0)

    sum_bytes_row = (
        db.query(func.coalesce(func.sum(Film.taille_octets), 0))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.taille_octets.isnot(None),
            Film.taille_octets > 0,
        )
        .scalar()
    )
    total_bytes = int(sum_bytes_row or 0)
    total_gib = total_bytes / (1024.0**3) if total_bytes else 0.0
    eur_per_day_storage = total_gib * 24.0 * rate_gib_h if rate_gib_h > 0 else 0.0
    eur_per_month_storage_30 = eur_per_day_storage * 30.0

    bucket_m = _month_bucket_expr(Film.date_ajout, dialect)
    bucket_d = _day_bucket_expr(Film.date_ajout, dialect)
    storage_rows = (
        db.query(bucket_m, func.coalesce(func.sum(Film.taille_octets), 0))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.taille_octets.isnot(None),
            Film.taille_octets > 0,
            Film.date_ajout >= since,
        )
        .group_by(bucket_m)
        .order_by(bucket_m)
        .all()
    )
    bytes_by_month: Dict[str, int] = {}
    for b, nbytes in storage_rows:
        lab = _billing_month_label(b, dialect)
        if lab:
            bytes_by_month[lab] = int(nbytes or 0)

    first_month = (now - timedelta(days=32 * months)).strftime("%Y-%m")[:7]
    current_month = now.strftime("%Y-%m")[:7]
    month_axis = _iter_month_labels(first_month[:7], current_month)
    cumulative_bytes = _catalog_bytes_before(db, since)
    storage_monthly: List[Dict[str, Any]] = []
    prev_gib_end = cumulative_bytes / (1024.0**3)
    for lab in month_axis:
        cumulative_bytes += int(bytes_by_month.get(lab, 0))
        gib_end = cumulative_bytes / (1024.0**3)
        y, m = int(lab[:4]), int(lab[5:7])
        dim = calendar.monthrange(y, m)[1]
        gib_start = prev_gib_end
        if rate_gib_h > 0:
            avg_gib = (gib_start + gib_end) / 2.0
            eur_month_storage = avg_gib * rate_gib_h * 24.0 * float(dim)
        else:
            eur_month_storage = 0.0
        storage_monthly.append(
            {
                "month": lab,
                "bytes_added_month": int(bytes_by_month.get(lab, 0)),
                "cumulative_bytes_catalog_proxy": cumulative_bytes,
                "cumulative_gib": round(gib_end, 4),
                "est_storage_eur_month_trapezoid_ht": round(eur_month_storage, 6),
            }
        )
        prev_gib_end = gib_end

    by_year: Dict[str, float] = {}
    for row in storage_monthly:
        yk = str(row["month"])[:4]
        by_year[yk] = by_year.get(yk, 0.0) + float(row["est_storage_eur_month_trapezoid_ht"])
    storage_yearly = [
        {"year": y, "est_storage_eur_sum_months_ht": round(v, 4)} for y, v in sorted(by_year.items())
    ]

    storage_day_rows = (
        db.query(bucket_d, func.coalesce(func.sum(Film.taille_octets), 0))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.taille_octets.isnot(None),
            Film.taille_octets > 0,
            Film.date_ajout.isnot(None),
            Film.date_ajout >= since_storage_day,
        )
        .group_by(bucket_d)
        .order_by(bucket_d)
        .all()
    )
    bytes_by_day: Dict[str, int] = {}
    for b, nbytes in storage_day_rows:
        dlab = _billing_day_label(b, dialect)
        if dlab:
            bytes_by_day[dlab] = int(nbytes or 0)

    first_day = since_storage_day.date().isoformat()
    current_day = now.date().isoformat()
    day_axis = _iter_day_labels(first_day, current_day)
    cumulative_bytes_day = _catalog_bytes_before(db, since_storage_day)
    storage_daily: List[Dict[str, Any]] = []
    for dlab in day_axis:
        cumulative_bytes_day += int(bytes_by_day.get(dlab, 0))
        gib_end = cumulative_bytes_day / (1024.0**3)
        if rate_gib_h > 0:
            eur_day_burn = gib_end * rate_gib_h * 24.0
        else:
            eur_day_burn = 0.0
        storage_daily.append(
            {
                "day": dlab,
                "bytes_added_day": int(bytes_by_day.get(dlab, 0)),
                "cumulative_bytes_catalog_proxy": cumulative_bytes_day,
                "cumulative_gib": round(gib_end, 4),
                "est_storage_eur_day_burn_ht": round(eur_day_burn, 6),
            }
        )

    vast_day_rows = (
        db.query(bucket_d, func.coalesce(func.sum(Film.duree_min), 0))
        .filter(
            Film.transcode_target == "vast",
            Film.statut == FilmStatut.disponible,
            Film.date_ajout >= since_day,
        )
        .group_by(bucket_d)
        .order_by(bucket_d)
        .all()
    )
    vast_eur_by_day: List[Dict[str, Any]] = []
    for b, mins in vast_day_rows:
        dlab = _billing_day_label(b, dialect)
        mval = int(mins or 0)
        usd = (mval / 60.0) * dph_usd if mval > 0 else 0.0
        vast_eur_by_day.append(
            {
                "day": dlab,
                "films_minutes_sum": mval,
                "est_vast_rental_usd": round(usd, 4),
                "est_vast_rental_eur": round(usd * usd_to_eur, 4),
            }
        )

    bucket_mv = _month_bucket_expr(Film.date_ajout, dialect)
    vast_month_rows = (
        db.query(bucket_mv, func.coalesce(func.sum(Film.duree_min), 0))
        .filter(
            Film.transcode_target == "vast",
            Film.statut == FilmStatut.disponible,
            Film.date_ajout >= since,
        )
        .group_by(bucket_mv)
        .order_by(bucket_mv)
        .all()
    )
    vast_eur_by_month: List[Dict[str, Any]] = []
    for b, mins in vast_month_rows:
        mlab = _billing_month_label(b, dialect)
        mval = int(mins or 0)
        usd = (mval / 60.0) * dph_usd if mval > 0 else 0.0
        vast_eur_by_month.append(
            {
                "month": mlab,
                "films_minutes_sum": mval,
                "est_vast_rental_usd": round(usd, 4),
                "est_vast_rental_eur": round(usd * usd_to_eur, 4),
            }
        )

    vast_films = (
        db.query(Film)
        .filter(Film.transcode_target == "vast", Film.statut == FilmStatut.disponible)
        .order_by(func.coalesce(Film.duree_min, 0).desc())
        .limit(40)
        .all()
    )
    vast_done_count = int(
        db.query(func.count(Film.id))
        .filter(Film.transcode_target == "vast", Film.statut == FilmStatut.disponible)
        .scalar()
        or 0
    )
    vast_minutes_all_row = (
        db.query(func.coalesce(func.sum(Film.duree_min), 0))
        .filter(Film.transcode_target == "vast", Film.statut == FilmStatut.disponible)
        .scalar()
    )
    vast_total_usd = (int(vast_minutes_all_row or 0) / 60.0) * dph_usd

    per_film: List[Dict[str, Any]] = []
    for f in vast_films:
        usd_one = _estimated_gpu_rental_usd(f.duree_min, dph_usd)
        u = float(usd_one) if usd_one is not None else 0.0
        per_film.append(
            {
                "id": f.id,
                "titre": (f.titre or "")[:512],
                "duree_min": f.duree_min,
                "taille_octets": f.taille_octets,
                "est_vast_rental_usd": usd_one,
                "est_vast_rental_eur": round(u * usd_to_eur, 4) if u else None,
            }
        )

    local_minutes_row = (
        db.query(func.coalesce(func.sum(Film.duree_min), 0))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.traitement == FilmTraitement.transcode,
            or_(Film.transcode_target.is_(None), Film.transcode_target == "local"),
        )
        .scalar()
    )
    local_minutes = int(local_minutes_row or 0)
    local_est_eur = (
        round(local_minutes * local_eur_per_min, 2) if local_eur_per_min > 0 and local_minutes else None
    )

    vast_total_eur = round(vast_total_usd * usd_to_eur, 2)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pricing": {
            "storage_eur_per_gib_hour_ht": rate_gib_h,
            "vast_max_dph_usd": dph_usd,
            "usd_to_eur_assumption": usd_to_eur,
            "local_transcode_eur_per_minute": local_eur_per_min,
        },
        "storage_now": {
            "total_bytes": total_bytes,
            "total_gib": round(total_gib, 4),
            "est_eur_per_day_storage_ht": round(eur_per_day_storage, 6),
            "est_eur_per_month_storage_30d_ht": round(eur_per_month_storage_30, 4),
        },
        "transcode_vast": {
            "films_disponible_count": vast_done_count,
            "est_total_rental_usd_upper": round(vast_total_usd, 2),
            "est_total_rental_eur_upper": vast_total_eur,
            "per_film_top": per_film,
        },
        "transcode_local": {
            "catalog_transcoded_minutes": local_minutes,
            "est_total_eur": local_est_eur,
        },
        "global_estimates": {
            "one_time_vast_transcode_eur_upper": vast_total_eur,
            "recurring_storage_eur_per_month_ht_30d": round(eur_per_month_storage_30, 4),
            "optional_local_transcode_eur": local_est_eur,
        },
        "series": {
            "months_requested": months,
            "storage_daily_days": storage_daily_days,
            "storage_monthly": storage_monthly,
            "storage_daily": storage_daily,
            "storage_cost_by_year_eur_ht": storage_yearly,
            "vast_transcode_eur_by_month": vast_eur_by_month,
            "vast_transcode_eur_by_day": vast_eur_by_day,
        },
        "disclaimers": [
            "Les montants Vast sont une borne haute (durée catalogue × VAST_MAX_DPH_PER_HOUR), pas la facture Vast.ai.",
            "Le stockage utilise la somme des taille_octets des fiches « disponibles », pas un inventaire S3 temps réel.",
            "Graphiques stockage (jour) : Gio cumulés par date d’ajout + coût journalier = Gio cumulés × €/(Gio·h) × 24 h (catalogue déjà présent avant la fenêtre inclus en point de départ).",
            "Synthèse annuelle : somme des coûts mensuels trapézoïdaux (€ HT).",
        ],
    }


@router.get("/system/stats")
def system_stats(_: User = Depends(require_admin)):
    enc = encoder_dict_for_api()
    return collect_system_stats(enc)


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    derniere_connexion: Optional[datetime]
    viewer_rank: Optional[str] = None
    signup_channel: Optional[str] = None
    registered_invite_code: Optional[str] = None
    registered_invite_note: Optional[str] = None

    class Config:
        from_attributes = True


class UsersListStatsOut(BaseModel):
    total: int
    admins: int
    viewers: int


class PaginatedUsersOut(BaseModel):
    items: List[UserOut]
    total: int
    page: int
    page_size: int
    stats: UsersListStatsOut


@router.get("/users", response_model=PaginatedUsersOut)
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    admins_count = int(
        db.query(func.count(User.id)).filter(User.role == UserRole.admin).scalar() or 0
    )
    viewers_count = int(
        db.query(func.count(User.id)).filter(User.role == UserRole.viewer).scalar() or 0
    )
    total_all = int(db.query(func.count(User.id)).scalar() or 0)
    stats = UsersListStatsOut(total=total_all, admins=admins_count, viewers=viewers_count)

    qn = (q or "").strip().lower()

    def _filtered_users_query():
        bq = db.query(User).outerjoin(
            InvitationCode,
            User.registered_via_invite_code_id == InvitationCode.id,
        )
        if qn:
            like = f"%{qn}%"
            bq = bq.filter(
                or_(
                    func.lower(User.username).like(like),
                    func.lower(User.email).like(like),
                    func.lower(func.coalesce(InvitationCode.code, "")).like(like),
                    func.lower(func.coalesce(InvitationCode.note, "")).like(like),
                    func.lower(func.coalesce(User.signup_channel, "")).like(like),
                )
            )
        return bq

    filtered_q = _filtered_users_query()
    filtered_total = int(filtered_q.count())
    total_pages = max(1, (filtered_total + page_size - 1) // page_size)
    safe_page = min(max(1, page), total_pages)
    rows = (
        _filtered_users_query()
        .order_by(User.id.asc())
        .offset((safe_page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    inv_ids = {u.registered_via_invite_code_id for u in rows if u.registered_via_invite_code_id}
    inv_map: dict[int, InvitationCode] = {}
    if inv_ids:
        for ic in db.query(InvitationCode).filter(InvitationCode.id.in_(inv_ids)).all():
            inv_map[ic.id] = ic
    out: list[UserOut] = []
    for u in rows:
        ic = (
            inv_map.get(u.registered_via_invite_code_id)
            if u.registered_via_invite_code_id
            else None
        )
        out.append(
            UserOut(
                id=u.id,
                username=u.username,
                email=u.email,
                role=u.role.value,
                is_active=u.is_active,
                derniere_connexion=u.derniere_connexion,
                viewer_rank=u.viewer_rank,
                signup_channel=u.signup_channel,
                registered_invite_code=ic.code if ic else None,
                registered_invite_note=ic.note if ic else None,
            )
        )
    return PaginatedUsersOut(
        items=out,
        total=filtered_total,
        page=safe_page,
        page_size=page_size,
        stats=stats,
    )


class CreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(max_length=128)
    password_confirm: str = Field(max_length=128)
    role: UserRole = UserRole.viewer
    viewer_rank: Optional[str] = None


@router.post("/users", response_model=UserOut)
def create_user(body: CreateUserBody, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    username_clean = body.username.strip()
    if len(username_clean) < 2:
        raise HTTPException(
            status_code=400,
            detail="L’identifiant doit contenir au moins 2 caractères.",
        )
    if db.query(User).filter(User.username == username_clean).first():
        raise HTTPException(status_code=400, detail="Identifiant déjà utilisé.")
    email_norm, email_err = validate_viewer_email(str(body.email))
    if email_err or not email_norm:
        raise HTTPException(status_code=400, detail=email_err or "Adresse e-mail invalide.")
    if (
        db.query(User)
        .filter(func.lower(User.email) == email_norm.lower())
        .first()
    ):
        raise HTTPException(status_code=400, detail="Cette adresse e-mail est déjà utilisée.")
    if body.password != body.password_confirm:
        raise HTTPException(
            status_code=400,
            detail="Les mots de passe ne correspondent pas.",
        )
    from core.password_policy import validate_password_strength
    from core.security import hash_password

    pw_err = validate_password_strength(
        body.password,
        username=username_clean,
        email=email_norm.lower(),
    )
    if pw_err:
        raise HTTPException(status_code=400, detail=pw_err)

    rank_val: Optional[str] = None
    viewer_prefs = None
    if body.role == UserRole.viewer:
        rank_val = _viewer_rank_for_new_viewer(body.viewer_rank)
        viewer_prefs = {"favorite_genres": []}
    u = User(
        username=username_clean,
        email=email_norm,
        hashed_password=hash_password(body.password),
        role=body.role,
        viewer_rank=rank_val,
        preferences=viewer_prefs,
        signup_channel="admin",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class PatchRoleBody(BaseModel):
    role: UserRole


class PatchViewerRankBody(BaseModel):
    viewer_rank: str


@router.patch("/users/{user_id}/viewer-rank")
def patch_viewer_rank(
    user_id: int,
    body: PatchViewerRankBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Not found")
    if u.role != UserRole.viewer:
        raise HTTPException(400, "Le rang ne s’applique qu’aux comptes viewer.")
    u.viewer_rank = _viewer_rank_update_value(body.viewer_rank)
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"ok": True, "viewer_rank": u.viewer_rank}


@router.patch("/users/{user_id}/role")
def patch_role(
    user_id: int,
    body: PatchRoleBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Not found")
    u.role = body.role
    if body.role == UserRole.viewer and not (u.viewer_rank or "").strip():
        u.viewer_rank = ViewerRank.bronze.value
    if body.role == UserRole.admin:
        u.viewer_rank = None
    db.commit()
    return {"ok": True}


@router.patch("/users/{user_id}/deactivate")
def deactivate(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "Cannot deactivate self")
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Not found")
    u.is_active = False
    u.deactivated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}


@router.patch("/users/{user_id}/activate")
def activate_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Not found")
    u.is_active = True
    u.deactivated_at = None
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/reset-invite-monthly-quota")
def reset_user_invite_monthly_quota(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Delete member-generated invitation codes for the current UTC month for this user
    and clear last_invite_at so they can generate a new code (spectator monthly quota).
    """
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Not found")
    deleted = reset_member_invite_quota_current_month(db, u)
    return {"ok": True, "deleted_invites": deleted}


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "Cannot delete self")
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Not found")
    db.delete(u)
    db.commit()


class SeriesSeasonMetaBody(BaseModel):
    series_key: str = Field(..., min_length=1, max_length=160)
    season_number: int = Field(..., ge=0)
    poster_path: Optional[str] = Field(None, max_length=2048)
    note: Optional[str] = Field(None, max_length=512)
    synopsis: Optional[str] = Field(None, max_length=32000)


@router.get("/series-seasons")
def admin_list_series_seasons(
    series_key: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    sk = (series_key or "").strip()
    if not sk:
        raise HTTPException(status_code=400, detail="series_key requis")
    rows = (
        db.query(SeriesSeasonMeta)
        .filter(SeriesSeasonMeta.series_key == sk)
        .order_by(SeriesSeasonMeta.season_number.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "series_key": r.series_key,
            "season_number": r.season_number,
            "poster_path": r.poster_path,
            "note": r.note,
            "synopsis": r.synopsis,
        }
        for r in rows
    ]


@router.post("/series-seasons")
def admin_upsert_series_season(
    body: SeriesSeasonMetaBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    sk = body.series_key.strip()
    sn = int(body.season_number)
    pp = (body.poster_path or "").strip() or None
    nt = (body.note or "").strip() or None
    sy = (body.synopsis or "").strip() or None
    row = (
        db.query(SeriesSeasonMeta)
        .filter(SeriesSeasonMeta.series_key == sk, SeriesSeasonMeta.season_number == sn)
        .first()
    )
    if row:
        row.poster_path = pp
        row.note = nt
        row.synopsis = sy
    else:
        row = SeriesSeasonMeta(
            series_key=sk,
            season_number=sn,
            poster_path=pp,
            note=nt,
            synopsis=sy,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "series_key": row.series_key,
        "season_number": row.season_number,
        "poster_path": row.poster_path,
        "note": row.note,
        "synopsis": row.synopsis,
    }


@router.delete("/series-seasons/{meta_id}", status_code=204)
def admin_delete_series_season(
    meta_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = db.get(SeriesSeasonMeta, meta_id)
    if not row:
        raise HTTPException(404, "Not found")
    db.delete(row)
    db.commit()


class SeriesShowPageBody(BaseModel):
    series_key: str = Field(..., min_length=1, max_length=160)
    poster_path: Optional[str] = Field(None, max_length=2048)
    hero_text: Optional[str] = Field(None, max_length=16000)


@router.get("/series-show")
def admin_get_series_show(
    series_key: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    sk = (series_key or "").strip()
    if not sk:
        raise HTTPException(status_code=400, detail="series_key requis")
    row = db.query(SeriesShowMeta).filter(SeriesShowMeta.series_key == sk).first()
    if not row:
        return {"id": None, "series_key": sk, "poster_path": None, "hero_text": None}
    return {
        "id": row.id,
        "series_key": row.series_key,
        "poster_path": row.poster_path,
        "hero_text": row.hero_text,
    }


@router.post("/series-show")
def admin_upsert_series_show(
    body: SeriesShowPageBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    sk = body.series_key.strip()
    pp = (body.poster_path or "").strip() or None
    ht = (body.hero_text or "").strip() or None
    row = db.query(SeriesShowMeta).filter(SeriesShowMeta.series_key == sk).first()
    if pp is None and ht is None:
        if row:
            db.delete(row)
            db.commit()
        return {"id": None, "series_key": sk, "poster_path": None, "hero_text": None}
    if row:
        row.poster_path = pp
        row.hero_text = ht
    else:
        row = SeriesShowMeta(series_key=sk, poster_path=pp, hero_text=ht)
        db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "series_key": row.series_key,
        "poster_path": row.poster_path,
        "hero_text": row.hero_text,
    }


class ViewerAnnouncementUpdateBody(BaseModel):
    message: str = ""
    duration_hours: int = Field(default=24, ge=1, le=720)


@router.get("/viewer-announcement")
def admin_get_viewer_announcement(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = _get_or_create_row(db)
    ends = row.ends_at
    return {
        "message": row.message or "",
        "ends_at": ends.strftime("%Y-%m-%dT%H:%M:%SZ") if ends else None,
        "active": _is_active(row),
    }


@router.put("/viewer-announcement")
@router.post("/viewer-announcement")
def admin_put_viewer_announcement(
    body: ViewerAnnouncementUpdateBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = _get_or_create_row(db)
    msg = (body.message or "").strip()
    if not msg:
        row.message = None
        row.ends_at = None
    else:
        row.message = msg
        row.ends_at = datetime.utcnow() + timedelta(hours=body.duration_hours)
    row.updated_at = datetime.utcnow()
    db.commit()
    ends = row.ends_at
    return {
        "ok": True,
        "message": row.message or "",
        "ends_at": ends.strftime("%Y-%m-%dT%H:%M:%SZ") if ends else None,
        "active": _is_active(row),
    }


_PLACEMENTS = frozenset({"login", "register", "both"})


def _normalize_auth_page_placement(raw: str) -> str:
    p = (raw or "").strip().lower()
    if p not in _PLACEMENTS:
        raise HTTPException(status_code=400, detail="placement must be login, register, or both")
    return p


def _serialize_auth_page_announcement_admin(row: AuthPageAnnouncement) -> Dict[str, Any]:
    ca = row.created_at
    ua = row.updated_at
    return {
        "id": row.id,
        "placement": row.placement,
        "title": row.title or "",
        "body": row.body or "",
        "is_active": bool(row.is_active),
        "sort_order": int(row.sort_order or 0),
        "created_at": ca.strftime("%Y-%m-%dT%H:%M:%SZ") if ca else None,
        "updated_at": ua.strftime("%Y-%m-%dT%H:%M:%SZ") if ua else None,
    }


class AuthPageAnnouncementCreateBody(BaseModel):
    placement: str
    title: str = ""
    body: str = Field(..., min_length=1, max_length=8000)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=-1000, le=1000)


class AuthPageAnnouncementPatchBody(BaseModel):
    placement: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = Field(default=None, max_length=8000)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=-1000, le=1000)


@router.get("/auth-page-announcements")
def admin_list_auth_page_announcements(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = (
        db.query(AuthPageAnnouncement)
        .order_by(AuthPageAnnouncement.sort_order.asc(), AuthPageAnnouncement.id.asc())
        .all()
    )
    return {"items": [_serialize_auth_page_announcement_admin(r) for r in rows]}


@router.post("/auth-page-announcements")
def admin_create_auth_page_announcement(
    body: AuthPageAnnouncementCreateBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    placement = _normalize_auth_page_placement(body.placement)
    msg = (body.body or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="body is required")
    now = datetime.now(timezone.utc)
    row = AuthPageAnnouncement(
        placement=placement,
        title=(body.title or "").strip() or None,
        body=msg,
        is_active=bool(body.is_active),
        sort_order=int(body.sort_order),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _serialize_auth_page_announcement_admin(row)


@router.patch("/auth-page-announcements/{announcement_id}")
def admin_patch_auth_page_announcement(
    announcement_id: int,
    body: AuthPageAnnouncementPatchBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = db.get(AuthPageAnnouncement, int(announcement_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    if body.placement is not None:
        row.placement = _normalize_auth_page_placement(body.placement)
    if body.title is not None:
        row.title = (body.title or "").strip() or None
    if body.body is not None:
        b = (body.body or "").strip()
        if not b:
            raise HTTPException(status_code=400, detail="body cannot be empty")
        row.body = b
    if body.is_active is not None:
        row.is_active = bool(body.is_active)
    if body.sort_order is not None:
        row.sort_order = int(body.sort_order)
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _serialize_auth_page_announcement_admin(row)


@router.delete("/auth-page-announcements/{announcement_id}")
def admin_delete_auth_page_announcement(
    announcement_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = db.get(AuthPageAnnouncement, int(announcement_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Announcement not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


def _strip_addr(s: Optional[str]) -> Optional[str]:
    t = (s or "").strip()
    return t if t else None


def _donation_campaign_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class DonationSettingsBody(BaseModel):
    goal_eur: float = Field(ge=0, le=1_000_000_000)
    address_btc: str = ""
    address_polygon: str = ""
    address_solana: str = ""
    address_xrp: str = ""
    address_tron: str = ""
    campaign_start_utc: Optional[datetime] = None
    campaign_end_utc: Optional[datetime] = None
    recurrence: str = "none"


@router.get("/donations")
def admin_get_donations(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = get_or_create_donation_settings(db)
    snap = row.snapshot_json if isinstance(row.snapshot_json, dict) else None
    return {
        "goal_eur": row.goal_eur if row.goal_eur is not None else 0.0,
        "address_btc": row.address_btc or "",
        "address_polygon": row.address_polygon or "",
        "address_solana": row.address_solana or "",
        "address_xrp": row.address_xrp or "",
        "address_tron": row.address_tron or "",
        "campaign_start_utc": _donation_campaign_iso(row.campaign_start_utc),
        "campaign_end_utc": _donation_campaign_iso(row.campaign_end_utc),
        "recurrence": normalize_recurrence(row.recurrence),
        "snapshot": snap,
        "updated_at": row.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if row.updated_at
        else None,
    }


@router.put("/donations")
@router.post("/donations")
def admin_put_donations(
    body: DonationSettingsBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    rec = normalize_recurrence(body.recurrence)
    if rec != RECURRENCE_NONE:
        if body.campaign_start_utc is None or body.campaign_end_utc is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Récurrence : renseignez la date et l’heure de début et de fin de campagne (UTC)."
                ),
            )
        if body.campaign_end_utc <= body.campaign_start_utc:
            raise HTTPException(
                status_code=400,
                detail="La fin de campagne doit être strictement après le début.",
            )
    row = get_or_create_donation_settings(db)
    row.goal_eur = float(body.goal_eur)
    row.address_btc = _strip_addr(body.address_btc)
    row.address_polygon = _strip_addr(body.address_polygon)
    row.address_solana = _strip_addr(body.address_solana)
    row.address_xrp = _strip_addr(body.address_xrp)
    row.address_tron = _strip_addr(body.address_tron)
    row.campaign_start_utc = body.campaign_start_utc
    row.campaign_end_utc = body.campaign_end_utc
    row.recurrence = rec
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    snap = row.snapshot_json if isinstance(row.snapshot_json, dict) else None
    return {
        "ok": True,
        "goal_eur": row.goal_eur,
        "address_btc": row.address_btc or "",
        "address_polygon": row.address_polygon or "",
        "address_solana": row.address_solana or "",
        "address_xrp": row.address_xrp or "",
        "address_tron": row.address_tron or "",
        "campaign_start_utc": _donation_campaign_iso(row.campaign_start_utc),
        "campaign_end_utc": _donation_campaign_iso(row.campaign_end_utc),
        "recurrence": rec,
        "snapshot": snap,
        "updated_at": row.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if row.updated_at
        else None,
    }


@router.post("/donations/refresh-balances")
def admin_refresh_donation_balances(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    row = get_or_create_donation_settings(db)
    addresses = {
        "btc": row.address_btc,
        "polygon": row.address_polygon,
        "solana": row.address_solana,
        "xrp": row.address_xrp,
        "tron": row.address_tron,
    }
    try:
        snap = compute_donation_snapshot(addresses)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    row.snapshot_json = snap
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "snapshot": snap}


@router.get("/vast/status")
def admin_vast_status(_: User = Depends(require_admin)):
    """Whether Vast.ai API key is configured (never return the key)."""
    s = get_settings()
    key_set = bool((getattr(s, "VAST_API_KEY", None) or "").strip())
    from core import vast_ai

    return {
        "configured": key_set,
        "api_base_url": (getattr(s, "VAST_API_BASE_URL", None) or "").strip()
        or "https://console.vast.ai/api/v0",
        "default_gpu_names": vast_ai.default_gpu_name_list(),
        "usable_gpu_names": vast_ai.usable_gpu_name_list(),
        "geolocation_excluded": vast_ai.parse_iso_country_codes(
            getattr(s, "VAST_EXCLUDE_GEOLOCATION_CODES", None) or ""
        ),
    }


@router.get("/vast/offers")
def admin_vast_offers(
    gpu: Optional[str] = Query(
        None,
        description="GPU names comma-separated; if omitted, see gpu_tier (VAST_DEFAULT / VAST_USABLE).",
    ),
    gpu_tier: str = Query(
        "default",
        description="Si gpu est omis : default | usable | all (union default + usable, sans doublons).",
    ),
    limit: int = Query(8, ge=1, le=25),
    max_dph: Optional[float] = Query(
        None,
        ge=0.001,
        le=500.0,
        description="Override VAST_MAX_DPH_PER_HOUR ($/h, dph_total lte).",
    ),
    max_bandwidth_usd_per_tb: Optional[float] = Query(
        None,
        ge=0.0,
        le=500.0,
        description="Override VAST_MAX_BANDWIDTH_USD_PER_TB; 0 = no inet cost cap in query.",
    ),
    verified_only: bool = Query(
        True,
        description="Si true, ne retourne que les offres explicitement vérifiées (verified ≠ false).",
    ),
    min_inet_down_mbps: Optional[float] = Query(
        None,
        ge=0.0,
        le=20000.0,
        description="Débit descendant min. (Mb/s, API Vast). Omis = env VAST_MIN_INET_DOWN_MBPS ; 0 = sans filtre.",
    ),
    min_inet_up_mbps: Optional[float] = Query(
        None,
        ge=0.0,
        le=20000.0,
        description="Débit montant min. (Mb/s). Omis = env VAST_MIN_INET_UP_MBPS ; 0 = sans filtre.",
    ),
    exclude_geolocation: Optional[str] = Query(
        None,
        description="Codes pays ISO (A2) à exclure, virgules. Omis = env VAST_EXCLUDE_GEOLOCATION_CODES ; chaîne vide = aucune exclusion.",
    ),
    _: User = Depends(require_admin),
):
    """Search rentable GPU offers on Vast.ai (test / ops). Requires VAST_API_KEY."""
    from core import vast_ai

    if not (get_settings().VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY non configurée. Voir docker/env.example.",
        )
    if (gpu or "").strip():
        names = [x.strip() for x in (gpu or "").split(",") if x.strip()]
    else:
        names = vast_ai.vast_gpu_names_for_tier(gpu_tier)
    if not names:
        raise HTTPException(
            status_code=400,
            detail="Aucun nom de GPU à rechercher. "
            "Pour gpu_tier=usable, définissez VAST_USABLE_GPU_NAMES ou passez gpu=… explicitement.",
        )
    try:
        s = get_settings()
        search_kw: Dict[str, Any] = {
            "limit": limit,
            "max_dph_per_hour": max_dph,
            "max_bandwidth_usd_per_tb": max_bandwidth_usd_per_tb,
            "min_inet_down_mbps": min_inet_down_mbps,
            "min_inet_up_mbps": min_inet_up_mbps,
        }
        if exclude_geolocation is not None:
            search_kw["exclude_geolocation_codes"] = vast_ai.parse_iso_country_codes(
                exclude_geolocation
            )
        rows = vast_ai.search_offers(names, **search_kw)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception("admin_vast_offers")
        raise HTTPException(status_code=502, detail=str(e)[:2000]) from e
    if verified_only:
        rows = [r for r in rows if isinstance(r, dict) and r.get("verified") is not False]
    dph_applied = float(max_dph) if max_dph is not None else float(s.VAST_MAX_DPH_PER_HOUR)
    bw_applied = (
        float(max_bandwidth_usd_per_tb)
        if max_bandwidth_usd_per_tb is not None
        else float(s.VAST_MAX_BANDWIDTH_USD_PER_TB)
    )
    eff_down = (
        float(min_inet_down_mbps)
        if min_inet_down_mbps is not None
        else float(getattr(s, "VAST_MIN_INET_DOWN_MBPS", 0.0) or 0.0)
    )
    eff_up = (
        float(min_inet_up_mbps)
        if min_inet_up_mbps is not None
        else float(getattr(s, "VAST_MIN_INET_UP_MBPS", 0.0) or 0.0)
    )
    return {
        "offers": rows,
        "count": len(rows),
        "filters": {
            "max_dph_per_hour": dph_applied,
            "max_bandwidth_usd_per_tb": bw_applied,
            "inet_cost_cap_usd_per_gb": (bw_applied / 1024.0) if bw_applied > 0 else None,
            "vast_bundles_verified_only": bool(getattr(s, "VAST_BUNDLES_VERIFIED_ONLY", True)),
            "verified_only": verified_only,
            "min_inet_down_mbps": eff_down,
            "min_inet_up_mbps": eff_up,
            "gpu_names": names,
            "gpu_tier": None if (gpu or "").strip() else (gpu_tier or "default").strip().lower(),
            "geolocation_notin": (
                vast_ai.parse_iso_country_codes(exclude_geolocation)
                if exclude_geolocation is not None
                else vast_ai.parse_iso_country_codes(
                    getattr(s, "VAST_EXCLUDE_GEOLOCATION_CODES", None) or ""
                )
            ),
        },
    }


@router.delete("/vast/instances/{instance_id}")
def admin_vast_destroy_instance(
    instance_id: int,
    _: User = Depends(require_admin),
):
    """Destroy a Vast instance by id (same id as new_contract when created). Test cleanup only."""
    from core import vast_ai

    if not (get_settings().VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY non configurée.",
        )
    try:
        out = vast_ai.destroy_instance(instance_id)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("admin_vast_destroy_instance")
        raise HTTPException(status_code=502, detail=str(e)[:2000]) from e
    return out


class VastTestCreateBody(BaseModel):
    """Create a short-lived Vast GPU instance for connectivity / nvidia-smi smoke test."""

    offer_id: int = Field(..., ge=1)
    label: str = Field(default="redwood-vast-test", max_length=80)
    disk_gb: int = Field(default=24, ge=8, le=200)
    image: str = Field(
        default="nvidia/cuda:12.3.1-base-ubuntu22.04",
        max_length=512,
    )


@router.post("/vast/test-instance")
def admin_vast_create_test_instance(
    body: VastTestCreateBody,
    _: User = Depends(require_admin),
):
    """
    Rent one instance on a chosen offer and run `nvidia-smi` on start (SSH image).
    Costs money while running — destroy with DELETE /vast/instances/{id}.
    """
    from core import vast_ai

    if not (get_settings().VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY non configurée.",
        )
    try:
        raw = vast_ai.create_instance(
            body.offer_id,
            image=body.image.strip(),
            disk_gb=body.disk_gb,
            runtype="ssh_direct",
            label=body.label.strip() or "redwood-vast-test",
            onstart="nvidia-smi || true",
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("admin_vast_create_test_instance")
        raise HTTPException(status_code=502, detail=str(e)[:2000]) from e
    iid = raw.get("new_contract") if isinstance(raw, dict) else None
    return {
        "ok": True,
        "instance_id": iid,
        "raw": raw,
        "hint": "DELETE /api/admin/vast/instances/{instance_id} pour détruire l’instance.",
    }


_VAST_TEST_VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}


class VastTranscodeRetryBody(BaseModel):
    """Retry Vast transcode using the same S3 vast-test input (film pipeline or explicit token)."""

    film_id: Optional[int] = Field(default=None, ge=1)
    job_token: Optional[str] = Field(default=None, max_length=64)
    src_ext: Optional[str] = Field(default=None, max_length=16)


def _film_vast_retryable(f: Film) -> bool:
    if (getattr(f, "transcode_target", None) or "local").lower() != "vast":
        return False
    if f.statut != FilmStatut.erreur:
        return False
    jt = (getattr(f, "vast_pending_job_token", None) or "").strip()
    se = (getattr(f, "vast_pending_input_ext", None) or "").strip()
    if not jt or not se:
        return False
    ext = se if se.startswith(".") else f".{se}"
    key = f"vast-test/{jt}/input{ext}"
    try:
        sz = object_size_or_none(key)
        return sz is not None and int(sz) > 4096
    except Exception:
        return False


@router.post("/transcode/vast/retry")
def admin_transcode_vast_retry(
    body: VastTranscodeRetryBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Re-queue vast_transcode_test_task with an existing S3 input under vast-test/{job_token}/.
    Use film_id after a worker failure (input kept on S3), or job_token + src_ext for manual tests.
    """
    s = get_settings()
    if not (s.VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY non configurée.",
        )
    film_row: Optional[Film] = None
    job_token = (body.job_token or "").strip()
    src_ext = (body.src_ext or "").strip()
    oid: Optional[int] = None
    film_title: Optional[str] = None
    fid_for_task: Optional[int] = None

    if body.film_id is not None:
        film_row = db.get(Film, int(body.film_id))
        if not film_row:
            raise HTTPException(status_code=404, detail="Film introuvable.")
        if (film_row.transcode_target or "local").lower() != "vast":
            raise HTTPException(
                status_code=400,
                detail="Ce film n’est pas configuré pour le transcodage Vast.",
            )
        if film_row.statut != FilmStatut.erreur:
            raise HTTPException(
                status_code=409,
                detail=f"Relance impossible : le film doit être en erreur (statut actuel : {film_row.statut.value}).",
            )
        jt = (getattr(film_row, "vast_pending_job_token", None) or "").strip()
        se = (getattr(film_row, "vast_pending_input_ext", None) or "").strip()
        if not jt or not se:
            raise HTTPException(
                status_code=409,
                detail="Aucune entrée S3 Vast enregistrée pour ce film (token manquant). Réimportez le torrent.",
            )
        job_token = jt
        src_ext = se if se.startswith(".") else f".{se}"
        vo = getattr(film_row, "vast_offer_id", None)
        oid = int(vo) if vo is not None and int(vo) > 0 else None
        film_title = (film_row.titre or "").strip() or None
        fid_for_task = int(film_row.id)
    else:
        if not job_token or not src_ext:
            raise HTTPException(
                status_code=400,
                detail="Fournir film_id, ou bien job_token et src_ext (ex. .mkv).",
            )
        src_ext = src_ext if src_ext.startswith(".") else f".{src_ext}"

    input_key = f"vast-test/{job_token}/input{src_ext}"
    try:
        sz = object_size_or_none(input_key)
    except Exception as e:
        logger.exception("transcode vast retry: head input")
        raise HTTPException(status_code=502, detail=str(e)[:2000]) from e
    if sz is None or int(sz) <= 4096:
        raise HTTPException(
            status_code=409,
            detail=f"Fichier source introuvable ou trop petit sur S3 ({input_key}).",
        )

    output_key = f"vast-test/{job_token}/output.mp4"
    progress_key = f"vast-test/{job_token}/remote_progress.txt"
    for k in (output_key, progress_key):
        try:
            delete_object_key(k)
        except Exception:
            pass

    try:
        async_res = vast_transcode_test_task.delay(job_token, src_ext, oid, fid_for_task)
    except Exception as e:
        logger.exception("transcode vast retry: Celery enqueue")
        raise HTTPException(
            status_code=503,
            detail="Impossible de lancer la tâche Celery (Redis / worker).",
        ) from e

    store_job_envelope(
        async_res.id,
        job_token,
        src_ext,
        film_id=fid_for_task,
        film_title=film_title,
        source="vast_retry",
    )
    if film_row is not None:
        film_row.statut = FilmStatut.en_cours
        film_row.erreur_message = None
        film_row.pipeline_progress = 12
        film_row.traitement = FilmTraitement.transcode
        film_row.pipeline_celery_task_id = async_res.id
        film_row.pipeline_celery_task_kind = "vast_transcode"
        db.commit()

    return {
        "task_id": async_res.id,
        "job_token": job_token,
        "s3_input_key": input_key,
        "offer_id": oid,
        "film_id": fid_for_task,
    }


@router.post("/transcode/vast")
async def admin_transcode_vast_upload(
    file: UploadFile = File(...),
    offer_id: Optional[int] = Form(None),
    _: User = Depends(require_admin),
):
    """
    Upload one video to S3 under vast-test/{token}/ and queue a Celery job that rents a Vast GPU,
    runs ffmpeg (NVENC) on the instance, uploads the MP4 back to S3, then destroys the instance.
    Requires VAST_API_KEY and S3 (same as production uploads).
    """
    s = get_settings()
    if not (s.VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY non configurée.",
        )
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _VAST_TEST_VIDEO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Extension non prise en charge ({', '.join(sorted(_VAST_TEST_VIDEO_EXT))}).",
        )
    try:
        path, _size = await save_upload_stream(file)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        logger.exception("transcode vast: save upload failed")
        raise HTTPException(
            status_code=507,
            detail=f"Écriture du fichier impossible : {e}",
        ) from e

    job_token = uuid.uuid4().hex
    s3_key = f"vast-test/{job_token}/input{ext}"
    try:
        upload_file(path, s3_key)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        logger.exception("transcode vast: S3 upload")
        raise HTTPException(status_code=502, detail=str(e)[:2000]) from e
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    oid = int(offer_id) if offer_id is not None and int(offer_id) > 0 else None
    try:
        async_res = vast_transcode_test_task.delay(job_token, ext, oid)
    except Exception as e:
        logger.exception("transcode vast: Celery enqueue")
        raise HTTPException(
            status_code=503,
            detail="Impossible de lancer la tâche Celery (Redis / worker).",
        ) from e
    store_job_envelope(
        async_res.id,
        job_token,
        ext,
        film_title=(file.filename or "upload")[:512],
        source="admin_upload",
    )
    return {
        "task_id": async_res.id,
        "job_token": job_token,
        "s3_input_key": s3_key,
        "offer_id": oid,
    }


@router.get("/transcode/vast/status/{task_id}")
def admin_transcode_vast_status(
    task_id: str,
    _: User = Depends(require_admin),
):
    """Poll Celery state + PROGRESS meta for POST /transcode/vast."""
    from celery.result import AsyncResult

    from worker.tasks import app as celery_app

    res = AsyncResult(task_id, app=celery_app)
    # Celery 5.x: ready and successful are methods — must call them (res.ready is always truthy).
    ready = bool(res.ready())
    out: dict[str, Any] = {
        "task_id": task_id,
        "state": res.state,
        "ready": ready,
    }
    if isinstance(res.info, dict) and res.state in ("PROGRESS", "STARTED", "RETRY"):
        out["meta"] = res.info
    if ready:
        if res.successful():
            out["result"] = res.result
        else:
            err = res.result
            out["error"] = str(err) if err is not None else "failure"
            if isinstance(res.info, dict) and res.info:
                out["meta"] = res.info
    return out


@router.get("/transcode/vast/queue")
def admin_transcode_vast_queue(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """
    List active/reserved Celery tasks for the Vast transcode worker task (inspect).
    Enriched with film id/title from Redis envelope (and DB fallback).
    """
    import ast

    from celery.result import AsyncResult

    from core.vast_transcode_cancel import read_job_envelope
    from worker.tasks import app as celery_app

    def _film_id_from_inspect_task(t: dict[str, Any]) -> Optional[int]:
        raw = t.get("args")
        if isinstance(raw, (list, tuple)) and len(raw) >= 4:
            x = raw[3]
            if isinstance(x, int) and x > 0:
                return x
        if isinstance(raw, str):
            try:
                parsed = ast.literal_eval(raw.strip())
                if isinstance(parsed, (list, tuple)) and len(parsed) >= 4:
                    x = parsed[3]
                    if isinstance(x, int) and x > 0:
                        return x
            except Exception:
                return None
        return None

    task_name = "worker.tasks.vast_transcode_test_task"
    items: list[dict[str, Any]] = []
    try:
        insp = celery_app.control.inspect(timeout=2.0)
        if not insp:
            return {"items": []}
        for kind, getter in (("active", insp.active), ("reserved", insp.reserved)):
            try:
                mapping = getter()
            except Exception:
                mapping = None
            if not mapping:
                continue
            for worker, task_list in mapping.items():
                if not task_list:
                    continue
                for t in task_list:
                    if not isinstance(t, dict):
                        continue
                    if t.get("name") != task_name:
                        continue
                    tid = t.get("id")
                    if not tid:
                        continue
                    entry: dict[str, Any] = {
                        "task_id": tid,
                        "worker": worker,
                        "args": str(t.get("args") or "")[:500],
                        "kind": kind,
                    }
                    env = read_job_envelope(str(tid))
                    if env:
                        jt = env.get("job_token")
                        if isinstance(jt, str) and jt.strip():
                            entry["job_token"] = jt.strip()
                        if env.get("film_id") is not None:
                            try:
                                entry["film_id"] = int(env["film_id"])
                            except (TypeError, ValueError):
                                pass
                        ft = env.get("film_title")
                        if isinstance(ft, str) and ft.strip():
                            entry["film_title"] = ft.strip()[:512]
                        src = env.get("source")
                        if isinstance(src, str) and src.strip():
                            entry["job_source"] = src.strip()[:64]
                    if entry.get("film_id") is None:
                        inferred = _film_id_from_inspect_task(t)
                        if inferred is not None:
                            entry["film_id"] = inferred
                    fid = entry.get("film_id")
                    if fid is not None and not entry.get("film_title"):
                        row = db.get(Film, int(fid))
                        if row and (row.titre or "").strip():
                            entry["film_title"] = (row.titre or "").strip()[:512]
                    try:
                        ar = AsyncResult(str(tid), app=celery_app)
                        if isinstance(ar.info, dict):
                            vi = ar.info.get("vast_instance_id")
                            if vi is not None:
                                entry["vast_instance_id"] = int(vi)
                    except Exception:
                        logger.debug("vast queue: no instance meta for task_id=%s", tid, exc_info=True)
                    items.append(entry)
    except Exception as e:
        logger.exception("transcode vast queue")
        return {"items": [], "error": str(e)[:500]}
    return {"items": items}


@router.post("/transcode/vast/cancel/{task_id}")
def admin_transcode_vast_cancel(
    task_id: str,
    _: User = Depends(require_admin),
):
    """
    Cancel a Vast transcode job: cooperative worker exit, destroy instance, S3 cleanup, Celery revoke.
    """
    s = get_settings()
    if not (s.VAST_API_KEY or "").strip():
        raise HTTPException(
            status_code=503,
            detail="VAST_API_KEY non configurée.",
        )
    from celery.result import AsyncResult

    from worker.tasks import app as celery_app

    res = AsyncResult(task_id, app=celery_app)
    if bool(res.ready()) and res.successful():
        raise HTTPException(
            status_code=409,
            detail="La tâche est déjà terminée avec succès (annulation impossible).",
        )
    try:
        return cancel_vast_transcode_test(celery_app, task_id)
    except Exception as e:
        logger.exception("transcode vast: cancel %s", task_id)
        raise HTTPException(status_code=502, detail=str(e)[:2000]) from e
