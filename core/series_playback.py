"""Resolve next/previous episode for series playback."""

from typing import Optional

from sqlalchemy.orm import Session

from db.models import ContentKind, Film, FilmStatut


def next_episode_id(db: Session, f: Film) -> Optional[int]:
    if f.content_kind != ContentKind.series_episode or not f.series_key:
        return None
    sno, eno = f.season_number, f.episode_number
    if sno is None or eno is None:
        return None
    n = (
        db.query(Film)
        .filter(
            Film.series_key == f.series_key,
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
            Film.series_key == f.series_key,
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
    p = (
        db.query(Film)
        .filter(
            Film.series_key == f.series_key,
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
            Film.series_key == f.series_key,
            Film.season_number < sno,
            Film.statut == FilmStatut.disponible,
        )
        .order_by(Film.season_number.desc(), Film.episode_number.desc())
        .first()
    )
    return p.id if p else None
