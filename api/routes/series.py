"""Series catalog: list shows and season-grouped episodes."""

from collections import defaultdict
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import get_current_user
from core.series_grouping import (
    equivalent_series_keys,
    normalize_series_group_key,
    series_catalog_group_key,
)
from db.models import ContentKind, Film, FilmStatut, SeriesSeasonMeta, SeriesShowMeta, User
from db.session import get_db

router = APIRouter(prefix="/api/series", tags=["series"])


def _series_matches_search(rep: Film, title: str, canon: str, sk_list: List[str], needle: str) -> bool:
    """Match query against show title, keys, episode row metadata, director, cast, synopsis."""
    from core.catalog_search import fold_matching_ascii, split_search_tokens

    tokens = split_search_tokens(needle)
    if not tokens:
        return True
    chunks = [
        (title or ""),
        " ".join(sk_list),
        canon,
        (rep.series_title or ""),
        (rep.titre or ""),
        (rep.realisateur or ""),
        (rep.synopsis or "") if rep.synopsis else "",
    ]
    if rep.acteurs:
        chunks.append(str(rep.acteurs))
    combined = " ".join(chunks)
    ch = fold_matching_ascii(combined).lower()
    return all(fold_matching_ascii(t).lower() in ch for t in tokens)


def _merge_show_meta_rows(rows: List[SeriesShowMeta]) -> tuple[Optional[str], Optional[str]]:
    """First non-empty poster_path and hero_text across equivalent series_key rows."""
    poster: Optional[str] = None
    hero: Optional[str] = None
    for r in rows:
        if poster is None and r.poster_path:
            s = str(r.poster_path).strip()
            if s:
                poster = s
        if hero is None and r.hero_text:
            t = str(r.hero_text).strip()
            if t:
                hero = t
        if poster is not None and hero is not None:
            break
    return poster, hero


def _build_series_catalog(db: Session, q: Optional[str] = None) -> List[dict[str, Any]]:
    rows = (
        db.query(Film.series_key)
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.content_kind == ContentKind.series_episode,
            Film.series_key.isnot(None),
        )
        .distinct()
        .all()
    )
    raw_keys = [sk for (sk,) in rows if sk]
    buckets: dict[str, List[str]] = defaultdict(list)
    for sk in raw_keys:
        buckets[normalize_series_group_key(sk)].append(sk)
    needle = (q or "").strip().lower()
    intermediate: List[dict[str, Any]] = []
    for canon, sk_list in buckets.items():
        if not canon:
            continue
        sk_list = list(dict.fromkeys(sk_list))
        rep = (
            db.query(Film)
            .filter(
                Film.series_key.in_(sk_list),
                Film.statut == FilmStatut.disponible,
            )
            .order_by(Film.season_number.asc().nulls_last(), Film.episode_number.asc().nulls_last())
            .first()
        )
        if not rep:
            continue
        show_rows = (
            db.query(SeriesShowMeta)
            .filter(SeriesShowMeta.series_key.in_(sk_list))
            .all()
        )
        show_poster, _ = _merge_show_meta_rows(show_rows)
        show_poster = show_poster or ""
        list_poster = show_poster or (rep.poster_path or "")
        title = rep.series_title or rep.titre
        if needle and not _series_matches_search(rep, title, canon, sk_list, needle):
            continue
        ep_count = (
            db.query(func.count(Film.id))
            .filter(
                Film.series_key.in_(sk_list),
                Film.statut == FilmStatut.disponible,
                Film.content_kind == ContentKind.series_episode,
            )
            .scalar()
        )
        merge_key = series_catalog_group_key(rep.series_title, rep.titre) or canon
        intermediate.append(
            {
                "merge_key": merge_key,
                "series_key": canon,
                "title": title,
                "poster_path": list_poster or None,
                "episode_count": int(ep_count or 0),
                "_keys": list(sk_list),
            }
        )
    merged: dict[str, dict[str, Any]] = {}
    for it in intermediate:
        mk = str(it["merge_key"])
        if mk not in merged:
            merged[mk] = {
                "series_key": it["series_key"],
                "title": it["title"],
                "poster_path": it["poster_path"],
                "episode_count": it["episode_count"],
                "_keys": set(it["_keys"]),
            }
        else:
            cur = merged[mk]
            cur["episode_count"] = int(cur["episode_count"]) + int(it["episode_count"])
            cur["_keys"].update(it["_keys"])
            if str(it["series_key"]) < str(cur["series_key"]):
                cur["series_key"] = it["series_key"]
                cur["title"] = it["title"]
            if it.get("poster_path") and not cur.get("poster_path"):
                cur["poster_path"] = it["poster_path"]
    for cur in merged.values():
        sk_u = list(cur["_keys"])
        del cur["_keys"]
        sc = (
            db.query(func.count(func.distinct(func.coalesce(Film.season_number, 0))))
            .filter(
                Film.series_key.in_(sk_u),
                Film.statut == FilmStatut.disponible,
                Film.content_kind == ContentKind.series_episode,
            )
            .scalar()
        )
        cur["season_count"] = int(sc or 0)
    out = list(merged.values())
    out.sort(key=lambda x: (x.get("title") or "").lower())
    return out


@router.get("")
def list_series(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Optional[str] = None,
    limit: Optional[int] = Query(
        None,
        ge=1,
        le=100,
        description="When set, return at most `limit` items after `offset` (stable title sort).",
    ),
    offset: int = Query(0, ge=0, description="Skip this many catalog items when `limit` is set."),
):
    rows = _build_series_catalog(db, q)
    if limit is not None:
        return rows[offset : offset + limit]
    return rows


@router.get("/recent")
def series_recent(
    limit: int = Query(16, ge=1, le=48),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Shows ordered by most recent episode addition (max date_ajout per series group).
    Each item includes date_ajout (UTC) for the latest episode in that show.
    """
    agg = (
        db.query(Film.series_key, func.max(Film.date_ajout).label("mx"))
        .filter(
            Film.statut == FilmStatut.disponible,
            Film.content_kind == ContentKind.series_episode,
            Film.series_key.isnot(None),
        )
        .group_by(Film.series_key)
        .all()
    )
    canon_to_mx: dict[str, datetime] = {}
    for sk, mx in agg:
        if not sk or mx is None:
            continue
        canon = normalize_series_group_key(sk)
        prev = canon_to_mx.get(canon)
        if prev is None or mx > prev:
            canon_to_mx[canon] = mx
    items = _build_series_catalog(db, None)
    for it in items:
        sk = it.get("series_key")
        if sk:
            it["date_ajout"] = canon_to_mx.get(sk)
    items.sort(
        key=lambda x: x.get("date_ajout") or datetime.min,
        reverse=True,
    )
    return items[:limit]


@router.get("/{series_key}/detail")
def series_detail(
    series_key: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    sk_list = equivalent_series_keys(db, series_key)
    rows = (
        db.query(Film)
        .filter(
            Film.series_key.in_(sk_list),
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
    meta_rows = (
        db.query(SeriesSeasonMeta)
        .filter(SeriesSeasonMeta.series_key.in_(sk_list))
        .order_by(SeriesSeasonMeta.season_number.asc())
        .all()
    )
    season_art: dict[str, dict[str, Any]] = {}
    for m in meta_rows:
        key = str(int(m.season_number))
        p = m.poster_path
        n = m.note
        s = m.synopsis
        if key not in season_art:
            season_art[key] = {
                "poster_path": p,
                "note": n,
                "synopsis": s,
            }
        else:
            cur = season_art[key]
            if not (cur.get("poster_path") or "").strip() and (p or "").strip():
                cur["poster_path"] = p
            if not (cur.get("note") or "").strip() and (n or "").strip():
                cur["note"] = n
            if not (cur.get("synopsis") or "").strip() and (s or "").strip():
                cur["synopsis"] = s
    show_rows = (
        db.query(SeriesShowMeta)
        .filter(SeriesShowMeta.series_key.in_(sk_list))
        .all()
    )
    show_poster, hero_text_val = _merge_show_meta_rows(show_rows)
    show_poster = show_poster or ""
    effective_poster = show_poster or rep.poster_path
    return {
        "series_key": normalize_series_group_key(series_key),
        "title": rep.series_title or rep.titre,
        "poster_path": effective_poster,
        "hero_text": hero_text_val,
        "synopsis": rep.synopsis,
        "note_tmdb": rep.note_tmdb,
        "seasons": seasons,
        "season_art": season_art,
    }
