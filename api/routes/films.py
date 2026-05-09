"""Public / authenticated film catalog routes."""

import random
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from api.deps import get_current_user, require_admin
from core.series_playback import next_episode_id, prev_episode_id
from core.s3 import presigned_stream_url
from core.tmdb import enrich_from_filename, movie_details, movie_trailers_youtube
from core.trailers_util import merge_trailer_lists, trailers_from_json_column
from db.models import ContentKind, Film, FilmStatut, User
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
    date_ajout: Optional[datetime] = None

    class Config:
        from_attributes = True


def _poster_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"https://image.tmdb.org/t/p/w500{path}"


def _genre_labels(genres_raw: Any) -> List[str]:
    """Normalize JSON genres to display names (strings, dicts with name, or comma-separated string)."""
    if not genres_raw:
        return []
    if isinstance(genres_raw, str):
        parts = [p.strip() for p in genres_raw.replace(";", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(genres_raw, list):
        out: List[str] = []
        for item in genres_raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                n = item.get("name") or item.get("Name")
                if isinstance(n, str) and n.strip():
                    out.append(n.strip())
        return out
    return []


def _film_genre_set(genres_raw: Any) -> set[str]:
    return {g.lower() for g in _genre_labels(genres_raw)}


@router.get("", response_model=List[FilmOut])
def list_films(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Optional[str] = None,
):
    query = db.query(Film).filter(
        Film.statut == FilmStatut.disponible,
        Film.content_kind == ContentKind.film,
    )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Film.titre.ilike(like), Film.realisateur.ilike(like)))
    films = query.order_by(Film.date_ajout.desc()).all()
    return films


@router.get("/featured", response_model=List[FilmOut])
def featured(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    films = (
        db.query(Film)
        .filter(Film.statut == FilmStatut.disponible, Film.content_kind == ContentKind.film)
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
        .filter(Film.statut == FilmStatut.disponible, Film.content_kind == ContentKind.film)
        .order_by(Film.date_ajout.desc())
        .limit(lim)
        .all()
    )


@router.get("/genres-summary")
def genres_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    films = (
        db.query(Film)
        .filter(Film.statut == FilmStatut.disponible, Film.content_kind == ContentKind.film)
        .all()
    )
    counts: dict[str, int] = {}
    for f in films:
        for g in _genre_labels(f.genres):
            counts[g] = counts.get(g, 0) + 1
    sample_poster: dict[str, str] = {}
    # Prefer films that have a poster so category tiles always get a backdrop when any title does.
    with_poster = [f for f in films if f.poster_path]
    for f in with_poster:
        pp = f.poster_path or ""
        for g in _genre_labels(f.genres):
            if g not in sample_poster and pp:
                sample_poster[g] = pp
    rows = [
        {"name": k, "count": v, "poster_path": sample_poster.get(k), "poster_url": _poster_url(sample_poster.get(k))}
        for k, v in counts.items()
    ]
    rows.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return rows


@router.get("/surprise-me", response_model=FilmOut)
def surprise_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Pick a catalog title weighted by the viewer's favorite genres (preferences)."""
    q = db.query(Film).filter(
        Film.statut == FilmStatut.disponible,
        Film.content_kind == ContentKind.film,
    )
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
            gset = _film_genre_set(f.genres)
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
        .filter(Film.statut == FilmStatut.disponible, Film.content_kind == ContentKind.film)
        .order_by(Film.date_ajout.desc())
        .all()
    )
    g = genre.strip().lower()
    out = []
    for f in films:
        if g in _film_genre_set(f.genres):
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
    playback = None
    if f.content_kind == ContentKind.series_episode and f.series_key:
        playback = {
            "series_key": f.series_key,
            "series_title": f.series_title,
            "season_number": f.season_number,
            "episode_number": f.episode_number,
            "next_episode_id": next_episode_id(db, f),
            "prev_episode_id": prev_episode_id(db, f),
        }
    manual_trailers = trailers_from_json_column(f.trailers_manual)
    tmdb_trailers: List[dict] = []
    if f.content_kind == ContentKind.film and f.tmdb_id:
        tmdb_trailers = movie_trailers_youtube(int(f.tmdb_id), limit=8)
    trailers = merge_trailer_lists(manual_trailers, tmdb_trailers)
    return {
        "id": f.id,
        "tmdb_id": f.tmdb_id,
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
        "statut": f.statut.value,
        "content_kind": f.content_kind.value,
        "playback": playback,
        "trailers": trailers,
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
