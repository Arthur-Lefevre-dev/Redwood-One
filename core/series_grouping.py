"""Unify series_key variants so the same TV show groups in the viewer catalog."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Dict, List, Optional, Set

from sqlalchemy.orm import Session

from core.imdbapi import normalize_imdb_tt_id
from db.models import ContentKind, Film, FilmStatut

# Strip trailing "S01E02 ..." / ".S01E02" from a title when inferring show name from episode line.
_EPISODE_SUFFIX_RE = re.compile(
    r"(?i)[\s._-]*[Ss]\d{1,4}[\s._-]*[Ee]\d{1,4}[\s._-]*.*$"
)


def _normalize_title_string(raw: str) -> str:
    """Case-fold, NFC, collapse spaces, strip trailing (YYYY) for stable comparisons."""
    if not raw or not str(raw).strip():
        return ""
    s = unicodedata.normalize("NFC", str(raw).strip())
    s = s.casefold()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*\((19|20)\d{2}\)\s*$", "", s)
    s = re.sub(r"\s+(19|20)\d{2}\s*$", "", s)
    return s.strip()


def normalize_series_group_key(series_key: Optional[str]) -> str:
    """
    Canonical form for grouping: ``imdb-tt123`` (IMDb) or ``tv-456`` (TMDB show id).
    Unknown shapes are returned unchanged so they still bucket together if identical.
    """
    if not series_key or not str(series_key).strip():
        return ""
    sk = str(series_key).strip()
    low = sk.lower()
    if low.startswith("imdb-"):
        nid = normalize_imdb_tt_id(sk[5:])
        return f"imdb-{nid}" if nid else sk
    if low.startswith("tv-"):
        rest = sk[3:].strip()
        if rest.isdigit():
            return f"tv-{int(rest)}"
        return sk
    return sk


def normalize_display_series_title(series_title: Optional[str]) -> str:
    """Normalized form of the admin « titre affiché de la série » for catalog grouping."""
    return _normalize_title_string((series_title or "").strip())


def normalize_show_name(series_title: Optional[str], titre: Optional[str]) -> str:
    """
    Inferred show label when ``series_title`` is missing: strip SxxEyy from ``titre``,
    then same folding as :func:`normalize_display_series_title`.
    """
    s = (series_title or "").strip()
    if not s:
        s = (titre or "").strip()
        s = _EPISODE_SUFFIX_RE.sub("", s)
        s = re.sub(r"[\._]+", " ", s)
    return _normalize_title_string(s)


def series_catalog_group_key(series_title: Optional[str], titre: Optional[str]) -> str:
    """
    Viewer catalog bucket: same « titre affiché de la série » (``series_title``) → same group.
    If ``series_title`` is empty, fall back to inferring from episode ``titre`` (legacy rows).
    """
    if (series_title or "").strip():
        return normalize_display_series_title(series_title)
    return normalize_show_name(None, titre)


def name_to_series_keys_map(db: Session) -> Dict[str, Set[str]]:
    """Maps normalized show name -> all ``series_key`` values that have at least one episode with that name."""
    rows = (
        db.query(Film.series_key, Film.series_title, Film.titre)
        .filter(
            Film.content_kind == ContentKind.series_episode,
            Film.statut == FilmStatut.disponible,
            Film.series_key.isnot(None),
        )
        .all()
    )
    m: Dict[str, Set[str]] = defaultdict(set)
    for sk, st, tit in rows:
        if not sk:
            continue
        nm = series_catalog_group_key(st, tit)
        if nm:
            m[nm].add(sk)
    return dict(m)


def equivalent_series_keys(db: Session, series_key: str) -> List[str]:
    """All distinct ``series_key`` values in the DB that belong to the same canonical show."""
    target = normalize_series_group_key(series_key)
    if not target:
        return [series_key] if series_key else []
    rows = (
        db.query(Film.series_key)
        .filter(
            Film.content_kind == ContentKind.series_episode,
            Film.statut == FilmStatut.disponible,
            Film.series_key.isnot(None),
        )
        .distinct()
        .all()
    )
    raw_keys = [sk for (sk,) in rows if sk]
    id_matches: List[str] = []
    if target:
        id_matches = [sk for sk in raw_keys if normalize_series_group_key(sk) == target]
    if not id_matches and series_key:
        id_matches = [series_key]
    id_matches = list(dict.fromkeys(id_matches))

    rep = (
        db.query(Film)
        .filter(
            Film.series_key.in_(id_matches),
            Film.content_kind == ContentKind.series_episode,
            Film.statut == FilmStatut.disponible,
        )
        .order_by(Film.season_number.asc().nulls_last(), Film.episode_number.asc().nulls_last())
        .first()
    )
    if not rep:
        return id_matches if id_matches else ([series_key] if series_key else [])

    nm = series_catalog_group_key(rep.series_title, rep.titre)
    if not nm:
        return id_matches if id_matches else ([series_key] if series_key else [])

    extra = name_to_series_keys_map(db).get(nm, set())
    merged = list(dict.fromkeys(list(id_matches) + list(extra)))
    return merged if merged else id_matches
