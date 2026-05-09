"""Public / authenticated film catalog routes."""

import random
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from api.deps import get_current_user, require_admin
from core.s3 import presigned_stream_url
from core.tmdb import enrich_from_filename, movie_details
from db.models import Film, FilmStatut, User
from db.session import get_db

router = APIRouter(prefix="/api/films", tags=["films"])


class FilmOut(BaseModel):
    id: int
    titre: str
    titre_original: Optional[str]
    annee: Optional[int]
    synopsis: Optional[str]
    genres: Optional[list]
    realisateur: Optional[str]
    poster_path: Optional[str]
    duree_min: Optional[int]
    resolution: Optional[str]
    statut: str

    class Config:
        from_attributes = True


def _poster_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"https://image.tmdb.org/t/p/w500{path}"


@router.get("", response_model=List[FilmOut])
def list_films(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Optional[str] = None,
):
    query = db.query(Film).filter(Film.statut == FilmStatut.disponible)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Film.titre.ilike(like), Film.realisateur.ilike(like)))
    films = query.order_by(Film.date_ajout.desc()).all()
    return films


@router.get("/featured", response_model=List[FilmOut])
def featured(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    films = (
        db.query(Film)
        .filter(Film.statut == FilmStatut.disponible)
        .order_by(Film.note_tmdb.desc().nulls_last(), Film.date_ajout.desc())
        .limit(12)
        .all()
    )
    return films


@router.get("/latest", response_model=List[FilmOut])
def latest_films(
    limit: int = 12,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    lim = max(1, min(48, limit))
    return (
        db.query(Film)
        .filter(Film.statut == FilmStatut.disponible)
        .order_by(Film.date_ajout.desc())
        .limit(lim)
        .all()
    )


@router.get("/genres-summary")
def genres_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    films = db.query(Film).filter(Film.statut == FilmStatut.disponible).all()
    counts: dict[str, int] = {}
    for f in films:
        for g in f.genres or []:
            if isinstance(g, str) and g.strip():
                k = g.strip()
                counts[k] = counts.get(k, 0) + 1
    rows = [{"name": k, "count": v} for k, v in counts.items()]
    rows.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return rows


@router.get("/surprise-me", response_model=FilmOut)
def surprise_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Pick a catalog title weighted by the viewer's favorite genres (preferences)."""
    q = db.query(Film).filter(Film.statut == FilmStatut.disponible)
    films = q.all()
    if not films:
        raise HTTPException(status_code=404, detail="Catalog empty")
    prefs: List[Any] = []
    if isinstance(user.preferences, dict):
        prefs = user.preferences.get("favorite_genres") or []
    if prefs and isinstance(prefs, list):
        scored: List[tuple[float, Film]] = []
        pl = [str(p).lower() for p in prefs]
        for f in films:
            gset = {str(x).lower() for x in (f.genres or []) if isinstance(x, str)}
            overlap = sum(1 for p in pl if p in gset)
            scored.append((overlap + random.random() * 0.01, f))
        scored.sort(key=lambda x: -x[0])
        top = [f for s, f in scored if s >= 1.0][: max(1, len(scored) // 3)]
        pool = top if top else [f for _, f in scored]
        return random.choice(pool)
    return random.choice(films)


@router.get("/by-genre")
def by_genre(genre: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    films = (
        db.query(Film)
        .filter(Film.statut == FilmStatut.disponible)
        .order_by(Film.date_ajout.desc())
        .all()
    )
    g = genre.lower()
    out = []
    for f in films:
        genres = f.genres or []
        if any(isinstance(x, str) and x.lower() == g for x in genres):
            out.append(f)
    return out


@router.get("/stats")
def stats(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    total = db.query(func.count(Film.id)).scalar() or 0
    ok = db.query(func.count(Film.id)).filter(Film.statut == FilmStatut.disponible).scalar() or 0
    err = db.query(func.count(Film.id)).filter(Film.statut == FilmStatut.erreur).scalar() or 0
    pending = db.query(func.count(Film.id)).filter(Film.statut == FilmStatut.en_cours).scalar() or 0
    return {"total": total, "disponible": ok, "erreur": err, "en_cours": pending}


@router.get("/{film_id}")
def film_detail(film_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Not found")
    return {
        "id": f.id,
        "titre": f.titre,
        "titre_original": f.titre_original,
        "annee": f.annee,
        "synopsis": f.synopsis,
        "genres": f.genres,
        "realisateur": f.realisateur,
        "acteurs": f.acteurs,
        "note_tmdb": f.note_tmdb,
        "poster_url": _poster_url(f.poster_path),
        "backdrop": None,
        "duree_min": f.duree_min,
        "resolution": f.resolution,
        "codec_video": f.codec_video,
        "statut": f.statut.value,
    }


@router.get("/{film_id}/stream-url")
def stream_url(film_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    f = db.get(Film, film_id)
    if not f or not f.s3_key:
        raise HTTPException(404, "Not found")
    url = presigned_stream_url(f.s3_key, expires=7200)
    return {"url": url, "expires_in": 7200}


@router.delete("/{film_id}", status_code=204)
def delete_film(film_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Not found")
    db.delete(f)
    db.commit()


@router.post("/{film_id}/refresh-tmdb")
def refresh_tmdb(film_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Not found")
    if f.tmdb_id:
        d = movie_details(f.tmdb_id)
        if d:
            f.synopsis = d.get("overview")
            f.genres = [g.get("name") for g in d.get("genres", []) if g.get("name")]
            f.note_tmdb = d.get("vote_average")
            f.poster_path = d.get("poster_path")
    else:
        data = enrich_from_filename(f.titre + ".mp4")
        for k, v in data.items():
            if hasattr(f, k) and v is not None:
                setattr(f, k, v)
    db.commit()
    return {"ok": True}
