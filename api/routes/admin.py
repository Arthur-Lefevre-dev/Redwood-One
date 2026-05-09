"""Admin-only routes: upload, queue, system, users, torrents."""

import base64
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.deps import require_admin
from api.routes.announcement import _get_or_create_row, _is_active
from api.routes.films import RefreshImdbApiBody, refresh_imdbapi as films_refresh_imdbapi
from config import get_settings
from core.catalog_sync import sync_s3_films_to_db
from core.trailers_util import trailers_from_admin_lines, trailers_from_json_column, trailers_to_watch_urls
from core.gpu_detect import encoder_dict_for_api
from core.system_stats import collect_system_stats
from core.member_invites import reset_member_invite_quota_current_month
from core.upload import save_upload_stream
from db.models import ContentKind, Film, FilmSource, FilmStatut, InvitationCode, User, UserRole
from db.session import get_db
from worker.tasks import download_torrent_task, process_film_task

router = APIRouter(prefix="/api/admin", tags=["admin"])


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


@router.get("/films")
def admin_list_films(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    q: Optional[str] = None,
):
    query = db.query(Film).order_by(Film.date_ajout.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Film.titre.ilike(like),
                Film.realisateur.ilike(like),
                Film.series_title.ilike(like),
                Film.series_key.ilike(like),
            )
        )
    rows = query.limit(500).all()
    return [
        {
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
        for f in rows
    ]


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
    process_film_task.delay(film.id, path)
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
    download_torrent_task.delay(film.id, body.magnet)
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
    download_torrent_task.delay(film.id, None, base64.b64encode(data).decode("ascii"))
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

    class Config:
        from_attributes = True


@router.get("/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return db.query(User).order_by(User.id.asc()).all()


class CreateUserBody(BaseModel):
    username: str
    email: EmailStr
    password: str
    role: UserRole = UserRole.viewer


@router.post("/users", response_model=UserOut)
def create_user(body: CreateUserBody, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(400, "Username exists")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(400, "Email exists")
    from core.security import hash_password

    u = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        role=body.role,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class PatchRoleBody(BaseModel):
    role: UserRole


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
