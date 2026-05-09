"""Resolve next/previous episode for series playback."""

from typing import List, Optional

from sqlalchemy.orm import Session

from core.series_grouping import equivalent_series_keys
from db.models import ContentKind, Film, FilmStatut


def _series_keys_for_film(db: Session, f: Film) -> List[str]:
    if not f.series_key:
        return []
    keys = equivalent_series_keys(db, f.series_key)
    return keys if keys else [f.series_key]


def next_episode_id(db: Session, f: Film) -> Optional[int]:
    if f.content_kind != ContentKind.series_episode or not f.series_key:
        return None
    sno, eno = f.season_number, f.episode_number
    if sno is None or eno is None:
        return None
    sks = _series_keys_for_film(db, f)
    n = (
        db.query(Film)
        .filter(
            Film.series_key.in_(sks),
            Film.season_number == sno,
            Film.episode_number > eno,
            Film.statut == FilmStatut.disponible,
        )
        .order_by(Film.episode_number.asc())
        .first()
    )
    if n:
        return n.id
    n = (
        db.query(Film)
        .filter(
            Film.series_key.in_(sks),
            Film.season_number > sno,
            Film.statut == FilmStatut.disponible,
        )
        .order_by(Film.season_number.asc(), Film.episode_number.asc())
        .first()
    )
    return n.id if n else None


def prev_episode_id(db: Session, f: Film) -> Optional[int]:
    if f.content_kind != ContentKind.series_episode or not f.series_key:
        return None
    sno, eno = f.season_number, f.episode_number
    if sno is None or eno is None:
        return None
    sks = _series_keys_for_film(db, f)
    p = (
        db.query(Film)
        .filter(
            Film.series_key.in_(sks),
            Film.season_number == sno,
            Film.episode_number < eno,
            Film.statut == FilmStatut.disponible,
        )
        .order_by(Film.episode_number.desc())
        .first()
    )
    if p:
        return p.id
    p = (
        db.query(Film)
        .filter(
            Film.series_key.in_(sks),
            Film.season_number < sno,
            Film.statut == FilmStatut.disponible,
        )
        .order_by(Film.season_number.desc(), Film.episode_number.desc())
        .first()
    )
    return p.id if p else None
