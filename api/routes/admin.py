"""Admin-only routes: upload, queue, system, users, torrents."""

import base64
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.deps import require_admin
from config import get_settings
from core.gpu_detect import encoder_dict_for_api
from core.system_stats import collect_system_stats
from core.upload import save_upload_stream
from db.models import Film, FilmSource, FilmStatut, User, UserRole
from db.session import get_db
from worker.tasks import download_torrent_task, process_film_task

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/films")
def admin_list_films(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    q: Optional[str] = None,
):
    query = db.query(Film).order_by(Film.date_ajout.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Film.titre.ilike(like), Film.realisateur.ilike(like)))
    rows = query.limit(500).all()
    return [
        {
            "id": f.id,
            "titre": f.titre,
            "realisateur": f.realisateur,
            "annee": f.annee,
            "taille_octets": f.taille_octets,
            "codec_video": f.codec_video,
            "traitement": f.traitement.value if f.traitement else None,
            "statut": f.statut.value,
            "poster_path": f.poster_path,
            "source": f.source.value,
            "erreur_message": f.erreur_message,
        }
        for f in rows
    ]


@router.post("/upload")
async def admin_upload(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        path, size = await save_upload_stream(file)
    except ValueError as e:
        raise HTTPException(400, str(e))

    film = Film(
        titre=Path(file.filename or "upload").stem,
        source=FilmSource.upload,
        statut=FilmStatut.en_cours,
        pipeline_progress=0,
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    process_film_task.delay(film.id, path)
    return {"job_id": film.id, "filename": file.filename, "size_bytes": size}


class TorrentMagnetBody(BaseModel):
    magnet: str


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
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    download_torrent_task.delay(film.id, body.magnet)
    return {"job_id": film.id}


@router.post("/torrents/file")
async def admin_torrent_file(
    torrent: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if not torrent.filename or not torrent.filename.lower().endswith(".torrent"):
        raise HTTPException(400, "Expected .torrent file")
    data = await torrent.read()
    film = Film(
        titre=Path(torrent.filename).stem,
        source=FilmSource.torrent,
        statut=FilmStatut.en_cours,
        pipeline_progress=0,
    )
    db.add(film)
    db.commit()
    db.refresh(film)
    # Celery JSON serializer cannot carry raw bytes; use base64 for the worker.
    download_torrent_task.delay(film.id, None, base64.b64encode(data).decode("ascii"))
    return {"job_id": film.id}


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
