"""Admin-only routes: upload, queue, system, users, torrents."""

import base64
import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from api.deps import require_admin
from core.donation_settings_store import get_or_create_donation_settings
from api.routes.announcement import _get_or_create_row, _is_active
from api.routes.films import RefreshImdbApiBody, refresh_imdbapi as films_refresh_imdbapi
from config import get_settings
from core.catalog_sync import sync_s3_films_to_db
from core.trailers_util import trailers_from_admin_lines, trailers_from_json_column, trailers_to_watch_urls
from core.gpu_detect import encoder_dict_for_api
from core.system_stats import collect_system_stats
from core.email_policy import validate_viewer_email
from core.donation_campaign import RECURRENCE_NONE, normalize_recurrence
from core.donation_service import compute_donation_snapshot
from core.member_invites import reset_member_invite_quota_current_month
from core.upload import save_upload_stream
from db.models import (
    ContentKind,
    Film,
    FilmSource,
    FilmStatut,
    InvitationCode,
    SeriesSeasonMeta,
    SeriesShowMeta,
    User,
    UserRole,
    ViewerRank,
)
from db.session import get_db
from worker.tasks import download_torrent_task, process_film_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
        process_film_task.delay(film_id, local_path)
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


def _enqueue_download_torrent_or_raise(
    film_id: int,
    db: Session,
    *,
    magnet: Optional[str] = None,
    torrent_b64: Optional[str] = None,
) -> None:
    try:
        download_torrent_task.delay(film_id, magnet, torrent_b64)
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
    episodes_total = ep_q.count()
    subq = (
        ep_q.order_by(None)
        .with_entities(Film.series_key)
        .filter(Film.series_key.isnot(None))
        .filter(Film.series_key != "")
        .distinct()
        .subquery()
    )
    series_shows_total = int(db.query(func.count()).select_from(subq).scalar() or 0)
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
):
    if content_kind not in ("film", "series_episode"):
        raise HTTPException(400, "content_kind must be 'film' or 'series_episode'")
    kind = ContentKind.film if content_kind == "film" else ContentKind.series_episode
    query = (
        _apply_admin_library_q(db.query(Film), q)
        .filter(Film.content_kind == kind)
        .order_by(Film.date_ajout.desc())
    )
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [_admin_film_row_dict(f) for f in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
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
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    _enqueue_process_film_or_raise(film.id, path, db)
    return {"job_id": film.id, "filename": file.filename, "size_bytes": size}


class TorrentMagnetBody(BaseModel):
    magnet: str
    content_kind: ContentKind = ContentKind.film


@router.post("/torrents")
def admin_torrent_magnet(
    body: TorrentMagnetBody,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if not body.magnet.startswith("magnet:?"):
        raise HTTPException(400, "Invalid magnet link")
    film = Film(
        titre="Torrent",
        source=FilmSource.torrent,
        statut=FilmStatut.en_cours,
        pipeline_progress=0,
        content_kind=body.content_kind,
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    _enqueue_download_torrent_or_raise(film.id, db, magnet=body.magnet)
    return {"job_id": film.id}


@router.post("/torrents/file")
async def admin_torrent_file(
    torrent: UploadFile = File(...),
    content_kind: str = Form("film"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if not torrent.filename or not torrent.filename.lower().endswith(".torrent"):
        raise HTTPException(400, "Expected .torrent file")
    data = await torrent.read()
    ck = _parse_upload_content_kind(content_kind)
    film = Film(
        titre=Path(torrent.filename).stem,
        source=FilmSource.torrent,
        statut=FilmStatut.en_cours,
        pipeline_progress=0,
        content_kind=ck,
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    # Celery JSON serializer cannot carry raw bytes; use base64 for the worker.
    _enqueue_download_torrent_or_raise(
        film.id,
        db,
        torrent_b64=base64.b64encode(data).decode("ascii"),
    )
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


@router.get("/invites")
def list_invites(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = db.query(InvitationCode).order_by(InvitationCode.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "code": r.code,
            "max_uses": r.max_uses,
            "uses": r.uses,
            "note": r.note,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


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
            }
        )
    return {"items": out}


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


@router.get("/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    rows = db.query(User).order_by(User.id.asc()).all()
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
    return out


class CreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(max_length=128)
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
