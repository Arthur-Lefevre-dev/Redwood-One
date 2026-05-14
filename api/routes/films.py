"""Public / authenticated film catalog routes."""

import logging
import random
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import Text, cast, func, literal, or_
from sqlalchemy.orm import Session

from api.deps import get_current_user, require_admin
from config import get_settings
from core.catalog_search import escape_like_pattern_fragment, split_search_tokens
from core.imdbapi import metadata_from_imdb_title_id, parse_imdb_tt
from core.series_grouping import normalize_series_group_key
from core.series_playback import next_episode_id, prev_episode_id
from core.s3 import delete_film_prefix, presigned_stream_url
from core.tmdb import (
    enrich_from_filename,
    movie_details,
    movie_trailers_youtube,
    tv_season_episode,
    tv_series_details,
)
from core.trailers_util import merge_trailer_lists, trailers_from_json_column
from db.models import ContentKind, Film, FilmStatut, User
from db.session import get_db

router = APIRouter(prefix="/api/films", tags=["films"])


def _filename_for_enrich(f: Film) -> str:
    """Prefer uploaded object basename so SxxEyy / year in the filename are preserved for metadata search."""
    key = (f.s3_key or "").strip()
    if key:
        name = PurePosixPath(key).name
        if name:
            return name
    t = (f.titre or "video").strip() or "video"
    lower = t.lower()
    if lower.endswith((".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v")):
        return t
    return f"{t}.mp4"

logger = logging.getLogger(__name__)


def _film_search_token_clause(token: str, *, use_unaccent: bool):
    """
    One search token must match at least one text field (OR).
    Multiple tokens are combined with AND at the query level so e.g. actor
    "François Damiens" still matches JSON cast where names are not contiguous.
    """
    if not token or not str(token).strip():
        return None
    pat = f"%{escape_like_pattern_fragment(str(token).strip())}%"
    esc = "\\"
    if use_unaccent:
        lit = literal(pat)
        ua = lambda col: func.unaccent(func.coalesce(cast(col, Text), ""))
        return or_(
            ua(Film.titre).ilike(func.unaccent(lit)),
            ua(Film.titre_original).ilike(func.unaccent(lit)),
            ua(Film.realisateur).ilike(func.unaccent(lit)),
            ua(Film.synopsis).ilike(func.unaccent(lit)),
            ua(Film.acteurs).ilike(func.unaccent(lit)),
            ua(Film.genres).ilike(func.unaccent(lit)),
        )
    return or_(
        Film.titre.ilike(pat, escape=esc),
        Film.titre_original.ilike(pat, escape=esc),
        Film.realisateur.ilike(pat, escape=esc),
        Film.synopsis.ilike(pat, escape=esc),
        cast(Film.acteurs, Text).ilike(pat, escape=esc),
        cast(Film.genres, Text).ilike(pat, escape=esc),
    )


class RefreshImdbApiBody(BaseModel):
    """Optional explicit IMDb id; if omitted, metadata is resolved from the file name (search)."""

    imdb_title_id: Optional[str] = None


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


def _actor_labels(acteurs_raw: Any) -> List[str]:
    """Normalize cast JSON to display names (strings or TMDB-style dicts)."""
    if not acteurs_raw:
        return []
    if isinstance(acteurs_raw, list):
        out: List[str] = []
        for item in acteurs_raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                n = item.get("name")
                if isinstance(n, str) and n.strip():
                    out.append(n.strip())
        return out
    return []


def _resolve_tmdb_trailers_cached(db: Session, f: Film) -> List[dict]:
    """
    Return TMDB YouTube trailer list from DB cache when fresh, else fetch videos API and persist.
    Manual entries (trailers_manual) are merged separately; this is only the TMDB slice.
    """
    settings = get_settings()
    days = max(0, settings.TMDB_TRAILERS_CACHE_DAYS)
    need_fetch = days == 0
    if not need_fetch:
        if not f.trailers_tmdb_cache or not f.trailers_tmdb_cached_at:
            need_fetch = True
        else:
            age_sec = (datetime.utcnow() - f.trailers_tmdb_cached_at).total_seconds()
            if age_sec >= days * 86400:
                need_fetch = True
    has_key = bool((settings.TMDB_API_KEY or "").strip())
    if need_fetch and has_key and f.tmdb_id:
        fresh = movie_trailers_youtube(int(f.tmdb_id), limit=1)
        f.trailers_tmdb_cache = fresh
        f.trailers_tmdb_cached_at = datetime.utcnow()
        db.add(f)
        db.commit()
        db.refresh(f)
        return fresh
    return trailers_from_json_column(f.trailers_tmdb_cache)


@router.get("", response_model=List[FilmOut])
def list_films(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Optional[str] = None,
    genre: Optional[str] = None,
    actor: Optional[str] = None,
    director: Optional[str] = None,
    tmdb_id: Optional[int] = Query(None, description="Filter by stored TMDB id (movie or TV ref)."),
    exclude_id: Optional[int] = Query(
        None,
        description="Exclude this film id (e.g. current title on film page).",
    ),
    limit: Optional[int] = Query(
        None,
        ge=1,
        le=100,
        description="When set, return a page of at most `limit` rows after `offset`.",
    ),
    offset: int = Query(0, ge=0, description="Skip this many rows when `limit` is set."),
):
    query = db.query(Film).filter(
        Film.statut == FilmStatut.disponible,
        Film.content_kind == ContentKind.film,
    )
    if tmdb_id is not None:
        query = query.filter(Film.tmdb_id == tmdb_id)
    if exclude_id is not None:
        query = query.filter(Film.id != exclude_id)
    if director and director.strip():
        query = query.filter(Film.realisateur.ilike(f"%{director.strip()}%"))
    if actor and actor.strip():
        query = query.filter(cast(Film.acteurs, Text).ilike(f"%{actor.strip()}%"))
    if genre and genre.strip():
        query = query.filter(cast(Film.genres, Text).ilike(f"%{genre.strip()}%"))
    if q and str(q).strip():
        bind = db.get_bind()
        use_unaccent = bool(
            bind.dialect.name == "postgresql" and bind.engine.info.get("has_unaccent")
        )
        for token in split_search_tokens(q):
            clause = _film_search_token_clause(token, use_unaccent=use_unaccent)
            if clause is not None:
                query = query.filter(clause)
    query = query.order_by(Film.date_ajout.desc())
    if limit is not None:
        return query.offset(offset).limit(limit).all()
    return query.all()


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
    # One random poster per genre (among titles that have a poster) for category tiles.
    poster_candidates: dict[str, list[str]] = {}
    with_poster = [f for f in films if f.poster_path]
    for f in with_poster:
        pp = (f.poster_path or "").strip()
        if not pp:
            continue
        for g in _genre_labels(f.genres):
            poster_candidates.setdefault(g, []).append(pp)
    sample_poster: dict[str, str] = {}
    for g, paths in poster_candidates.items():
        if paths:
            sample_poster[g] = random.choice(paths)
    rows = [
        {"name": k, "count": v, "poster_path": sample_poster.get(k), "poster_url": _poster_url(sample_poster.get(k))}
        for k, v in counts.items()
    ]
    rows.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return rows


@router.get("/directors-summary")
def directors_summary(
    limit: int = 40,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Distinct directors (realisateur) with film counts for browse chips."""
    lim = max(5, min(150, limit))
    rows = (
        db.query(Film.realisateur, func.count(Film.id))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.content_kind == ContentKind.film,
            Film.realisateur.isnot(None),
            Film.realisateur != "",
        )
        .group_by(Film.realisateur)
        .order_by(func.count(Film.id).desc())
        .limit(lim)
        .all()
    )
    return [{"name": n, "count": int(c)} for n, c in rows if n]


@router.get("/actors-summary")
def actors_summary(
    limit: int = 60,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Aggregated cast names from stored TMDB/IMDb metadata (JSON acteurs)."""
    lim = max(10, min(400, limit))
    films = (
        db.query(Film.acteurs)
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.content_kind == ContentKind.film,
            Film.acteurs.isnot(None),
        )
        .all()
    )
    counts: dict[str, int] = {}
    for (raw,) in films:
        for name in _actor_labels(raw):
            counts[name] = counts.get(name, 0) + 1
    rows = [{"name": k, "count": v} for k, v in counts.items()]
    rows.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return rows[:lim]


def _surprise_me_candidate_query(
    db: Session,
    *,
    genre: Optional[str] = None,
    actor: Optional[str] = None,
    director: Optional[str] = None,
    score_min: Optional[float] = None,
):
    """Base query for surprise-me pool; same filter semantics as list_films."""
    q = db.query(Film).filter(
        Film.statut == FilmStatut.disponible,
        Film.content_kind == ContentKind.film,
    )
    if director and director.strip():
        q = q.filter(Film.realisateur.ilike(f"%{director.strip()}%"))
    if actor and actor.strip():
        q = q.filter(cast(Film.acteurs, Text).ilike(f"%{actor.strip()}%"))
    if genre and genre.strip():
        q = q.filter(cast(Film.genres, Text).ilike(f"%{genre.strip()}%"))
    if score_min is not None:
        q = q.filter(Film.note_tmdb.isnot(None), Film.note_tmdb >= float(score_min))
    return q


@router.get("/surprise-me", response_model=FilmOut)
def surprise_me(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    genre: Optional[str] = None,
    actor: Optional[str] = None,
    director: Optional[str] = None,
    score_min: Optional[float] = Query(
        None,
        ge=0,
        le=10,
        description="Minimum TMDB vote_average (0–10) stored on the film row.",
    ),
):
    """Pick a catalog title weighted by the viewer's favorite genres (preferences).

    Optional filters narrow the pool (same rules as GET /api/films).
    """
    q = _surprise_me_candidate_query(
        db,
        genre=genre,
        actor=actor,
        director=director,
        score_min=score_min,
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
            "series_key": normalize_series_group_key(f.series_key),
            "series_title": f.series_title,
            "season_number": f.season_number,
            "episode_number": f.episode_number,
            "next_episode_id": next_episode_id(db, f),
            "prev_episode_id": prev_episode_id(db, f),
        }
    manual_trailers = trailers_from_json_column(f.trailers_manual)
    tmdb_trailers: List[dict] = []
    if f.content_kind == ContentKind.film and f.tmdb_id:
        tmdb_trailers = _resolve_tmdb_trailers_cached(db, f)
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
    try:
        delete_film_prefix(film_id, known_s3_key=f.s3_key)
    except Exception as e:
        logger.exception("s3 delete failed for film_id=%s", film_id)
        raise HTTPException(status_code=503, detail=str(e)) from e
    db.delete(f)
    db.commit()


@router.post("/{film_id}/refresh-tmdb")
def refresh_tmdb(film_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Not found")
    has_key = bool((get_settings().TMDB_API_KEY or "").strip())

    if f.content_kind == ContentKind.series_episode:
        applied = False
        if (
            f.tmdb_id
            and f.season_number is not None
            and f.episode_number is not None
        ):
            ep = tv_season_episode(int(f.tmdb_id), int(f.season_number), int(f.episode_number))
            show = tv_series_details(int(f.tmdb_id))
            if ep and show:
                f.synopsis = ep.get("overview")
                f.note_tmdb = ep.get("vote_average")
                still = ep.get("still_path")
                if still:
                    f.poster_path = still
                elif show.get("poster_path"):
                    f.poster_path = show.get("poster_path")
                ename = ep.get("name")
                if ename:
                    f.titre = ename
                f.genres = [g.get("name") for g in show.get("genres", []) if g.get("name")]
                if not f.series_title:
                    f.series_title = show.get("name")
                applied = True
        if not applied:
            data = enrich_from_filename(
                _filename_for_enrich(f),
                ContentKind.series_episode,
                metadata_provider="tmdb",
            )
            for k, v in data.items():
                if hasattr(f, k) and v is not None:
                    setattr(f, k, v)
        f.trailers_tmdb_cache = None
        f.trailers_tmdb_cached_at = None
    else:
        if f.tmdb_id:
            d = movie_details(f.tmdb_id)
            if d:
                f.synopsis = d.get("overview")
                f.genres = [g.get("name") for g in d.get("genres", []) if g.get("name")]
                f.note_tmdb = d.get("vote_average")
                f.poster_path = d.get("poster_path")
        else:
            data = enrich_from_filename(
                _filename_for_enrich(f),
                ContentKind.film,
                metadata_provider="tmdb",
            )
            for k, v in data.items():
                if hasattr(f, k) and v is not None:
                    setattr(f, k, v)
        tid = f.tmdb_id
        if tid and has_key:
            f.trailers_tmdb_cache = movie_trailers_youtube(int(tid), limit=1)
            f.trailers_tmdb_cached_at = datetime.utcnow()
        else:
            f.trailers_tmdb_cache = None
            f.trailers_tmdb_cached_at = None
    db.commit()
    return {"ok": True}


@router.post("/{film_id}/refresh-imdbapi")
def refresh_imdbapi(
    film_id: int,
    body: RefreshImdbApiBody = Body(default_factory=RefreshImdbApiBody),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Re-fetch metadata from imdbapi.dev: by ``imdb_title_id`` if sent, else search from file name."""
    f = db.get(Film, film_id)
    if not f:
        raise HTTPException(404, "Not found")

    raw_id = (body.imdb_title_id or "").strip()
    if raw_id:
        if not parse_imdb_tt(raw_id):
            raise HTTPException(
                status_code=400,
                detail="Identifiant IMDb invalide (attendu : tt suivi de chiffres, ex. tt1375666).",
            )
        data = metadata_from_imdb_title_id(raw_id)
        if not data:
            raise HTTPException(
                status_code=404,
                detail="Titre IMDb introuvable sur imdbapi.dev pour cet identifiant.",
            )
    else:
        data = enrich_from_filename(
            _filename_for_enrich(f),
            f.content_kind,
            metadata_provider="imdbapi",
        )

    for k, v in data.items():
        if hasattr(f, k) and v is not None:
            setattr(f, k, v)
    f.trailers_tmdb_cache = None
    f.trailers_tmdb_cached_at = None
    db.commit()
    return {"ok": True}
