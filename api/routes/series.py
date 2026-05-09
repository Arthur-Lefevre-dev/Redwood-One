"""Series catalog: list shows and season-grouped episodes."""

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import get_current_user
from db.models import ContentKind, Film, FilmStatut, User
from db.session import get_db

router = APIRouter(prefix="/api/series", tags=["series"])


@router.get("")
def list_series(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Optional[str] = None,
):
    keys = (
        db.query(Film.series_key)
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.content_kind == ContentKind.series_episode,
            Film.series_key.isnot(None),
        )
        .distinct()
        .all()
    )
    needle = (q or "").strip().lower()
    out: List[dict[str, Any]] = []
    for (sk,) in keys:
        if not sk:
            continue
        rep = (
            db.query(Film)
            .filter(
                Film.series_key == sk,
                Film.statut == FilmStatut.disponible,
            )
            .order_by(Film.season_number.asc().nulls_last(), Film.episode_number.asc().nulls_last())
            .first()
        )
        if not rep:
            continue
        title = rep.series_title or rep.titre
        if needle and needle not in (title or "").lower():
            continue
        ep_count = (
            db.query(func.count(Film.id))
            .filter(Film.series_key == sk, Film.statut == FilmStatut.disponible)
            .scalar()
        )
        out.append(
            {
                "series_key": sk,
                "title": title,
                "poster_path": rep.poster_path,
                "episode_count": int(ep_count or 0),
            }
        )
    out.sort(key=lambda x: (x.get("title") or "").lower())
    return out


@router.get("/{series_key}/detail")
def series_detail(
    series_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (
        db.query(Film)
        .filter(
            Film.series_key == series_key,
            Film.statut == FilmStatut.disponible,
            Film.content_kind == ContentKind.series_episode,
        )
        .order_by(Film.season_number.asc().nulls_last(), Film.episode_number.asc().nulls_last())
        .all()
    )
    if not rows:
        raise HTTPException(404, "Series not found")
    rep = rows[0]
    seasons: dict[str, list] = {}
    for f in rows:
        s = f.season_number if f.season_number is not None else 0
        k = str(int(s))
        if k not in seasons:
            seasons[k] = []
        seasons[k].append(
            {
                "id": f.id,
                "titre": f.titre,
                "episode_number": f.episode_number,
            }
        )
    for k in seasons:
        seasons[k].sort(key=lambda e: (e.get("episode_number") is None, e.get("episode_number") or 0))
    return {
        "series_key": series_key,
        "title": rep.series_title or rep.titre,
        "poster_path": rep.poster_path,
        "synopsis": rep.synopsis,
        "seasons": seasons,
    }
