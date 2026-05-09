"""Series catalog: list shows and season-grouped episodes."""

from collections import defaultdict
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
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


@router.get("")
def list_series(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Optional[str] = None,
):
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
        if needle:
            t = (title or "").lower()
            skl = " ".join(sk_list).lower()
            if needle not in t and needle not in skl and needle not in canon.lower():
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
