"""IMDb metadata via free API https://imdbapi.dev/ (Base: api.imdbapi.dev)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from config import get_settings
from db.models import ContentKind

logger = logging.getLogger(__name__)

# Import shared filename helpers (avoid duplicating season/show parsing).
from core.tmdb import (  # noqa: E402
    _clean_title_guess,
    _show_query_from_filename,
    parse_tv_season_episode,
)


def _base_url() -> str:
    return (get_settings().IMDBAPI_BASE_URL or "https://api.imdbapi.dev").rstrip("/")


def _get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    url = _base_url() + path
    try:
        with httpx.Client(timeout=25.0) as client:
            r = client.get(url, params=params or {})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.exception("imdbapi GET %s failed: %s", path, e)
        return None


def search_titles(query: str) -> List[Dict[str, Any]]:
    data = _get_json("/search/titles", {"query": query.strip()})
    if not data or not isinstance(data, dict):
        return []
    return list(data.get("titles") or [])


def get_title(title_id: str) -> Optional[Dict[str, Any]]:
    tid = (title_id or "").strip()
    if not tid.startswith("tt"):
        return None
    data = _get_json(f"/titles/{tid}")
    return data if isinstance(data, dict) else None


def _pick_by_type_and_year(
    titles: List[Dict[str, Any]], want_type: str, year_hint: Optional[int]
) -> Optional[Dict[str, Any]]:
    typed = [t for t in titles if (t.get("type") or "") == want_type]
    pool = typed or titles
    if year_hint and pool:
        for t in pool:
            if t.get("startYear") == year_hint:
                return t
    return pool[0] if pool else None


def _primary_image_url(detail: Dict[str, Any]) -> Optional[str]:
    img = detail.get("primaryImage")
    if isinstance(img, dict):
        u = img.get("url")
        if u:
            return str(u)
    return None


def _rating_value(detail: Dict[str, Any]) -> Optional[float]:
    r = detail.get("rating")
    if isinstance(r, dict) and r.get("aggregateRating") is not None:
        try:
            return float(r["aggregateRating"])
        except (TypeError, ValueError):
            return None
    return None


def _directors_line(detail: Dict[str, Any]) -> Optional[str]:
    dirs = detail.get("directors")
    if not isinstance(dirs, list) or not dirs:
        return None
    names = []
    for d in dirs[:3]:
        if isinstance(d, dict) and d.get("displayName"):
            names.append(d["displayName"])
    return ", ".join(names) if names else None


def fetch_credits_cast(title_id: str, limit: int = 12) -> List[str]:
    data = _get_json(f"/titles/{title_id.strip()}/credits")
    if not data or not isinstance(data, dict):
        return []
    out: List[str] = []
    for row in data.get("credits") or []:
        if len(out) >= limit:
            break
        cat = (row.get("category") or "").lower()
        if cat not in ("actor", "actress"):
            continue
        name = row.get("name") or {}
        if isinstance(name, dict) and name.get("displayName"):
            n = name["displayName"]
            if n not in out:
                out.append(n)
    return out


def find_episode_on_show(show_id: str, season: int, episode: int) -> Optional[Dict[str, Any]]:
    token: Optional[str] = None
    want_s, want_e = str(int(season)), int(episode)
    while True:
        params: Dict[str, Any] = {}
        if token:
            params["pageToken"] = token
        data = _get_json(f"/titles/{show_id.strip()}/episodes", params)
        if not data or not isinstance(data, dict):
            return None
        for ep in data.get("episodes") or []:
            if str(ep.get("season")) == want_s and int(ep.get("episodeNumber") or -1) == want_e:
                return ep if isinstance(ep, dict) else None
        token = data.get("nextPageToken")
        if not token:
            break
    return None


def _empty_film_fields(guess: str) -> Dict[str, Any]:
    return {
        "titre": guess,
        "tmdb_id": None,
        "imdb_title_id": None,
        "synopsis": None,
        "genres": [],
        "realisateur": None,
        "acteurs": [],
        "note_tmdb": None,
        "poster_path": None,
        "langue_originale": None,
        "annee": None,
        "series_title": None,
        "series_key": None,
    }


def _stars_to_cast(detail: Dict[str, Any], limit: int = 12) -> List[str]:
    stars = detail.get("stars")
    if not isinstance(stars, list):
        return []
    out: List[str] = []
    for s in stars[:limit]:
        if isinstance(s, dict) and s.get("displayName"):
            n = s["displayName"]
            if n not in out:
                out.append(n)
    return out


def _spoken_lang(detail: Dict[str, Any]) -> Optional[str]:
    langs = detail.get("spokenLanguages")
    if isinstance(langs, list) and langs:
        first = langs[0]
        if isinstance(first, dict) and first.get("code"):
            return str(first["code"])[:32]
    return None


def enrich_movie_from_filename(filename: str) -> Dict[str, Any]:
    guess = _clean_title_guess(filename)
    m = re.search(r"\b(19|20)\d{2}\b", Path(filename).name)
    year = int(m.group(0)) if m else None
    hits = search_titles(guess)
    hit = _pick_by_type_and_year(hits, "movie", year)
    if not hit:
        out = _empty_film_fields(guess)
        out["annee"] = year
        return out

    tid = hit.get("id")
    detail = get_title(str(tid)) if tid else None
    base = detail or hit
    genres = list(base.get("genres") or [])
    director = _directors_line(base) if isinstance(base, dict) else None
    cast: List[str] = []
    if tid:
        cast = fetch_credits_cast(str(tid))
    if isinstance(base, dict) and len(cast) < 4:
        for n in _stars_to_cast(base):
            if n not in cast:
                cast.append(n)
    img = _primary_image_url(base) if isinstance(base, dict) else None
    if not img and isinstance(hit, dict):
        img = _primary_image_url(hit)

    ay = base.get("startYear") if isinstance(base, dict) else None
    if ay is None:
        ay = hit.get("startYear")
    try:
        ay_int = int(ay) if ay is not None else year
    except (TypeError, ValueError):
        ay_int = year

    return {
        "tmdb_id": None,
        "imdb_title_id": str(tid) if tid else None,
        "titre": (base.get("primaryTitle") if isinstance(base, dict) else None)
        or hit.get("primaryTitle")
        or guess,
        "titre_original": (base.get("originalTitle") if isinstance(base, dict) else None)
        or hit.get("originalTitle"),
        "synopsis": base.get("plot") if isinstance(base, dict) else None,
        "genres": genres,
        "realisateur": director,
        "acteurs": cast,
        "note_tmdb": _rating_value(base) if isinstance(base, dict) else _rating_value(hit),
        "poster_path": img,
        "langue_originale": _spoken_lang(base) if isinstance(base, dict) else None,
        "annee": ay_int,
    }


def enrich_series_episode_from_filename(filename: str) -> Dict[str, Any]:
    parsed = parse_tv_season_episode(filename)
    guess = _clean_title_guess(filename)

    if not parsed:
        hits = search_titles(guess)
        hit = _pick_by_type_and_year(hits, "tvSeries", None)
        if not hit:
            out = _empty_film_fields(guess)
            return out
        tid = hit.get("id")
        detail = get_title(str(tid)) if tid else None
        base = detail or hit
        genres = list(base.get("genres") or []) if isinstance(base, dict) else []
        stitle = (
            base.get("primaryTitle") if isinstance(base, dict) else None
        ) or hit.get("primaryTitle") or guess
        img = _primary_image_url(base) if isinstance(base, dict) else _primary_image_url(hit)
        rel = base.get("startYear") if isinstance(base, dict) else hit.get("startYear")
        try:
            ay = int(rel) if rel is not None else None
        except (TypeError, ValueError):
            ay = None
        return {
            "tmdb_id": None,
            "imdb_title_id": str(tid) if tid else None,
            "titre": guess,
            "titre_original": None,
            "synopsis": base.get("plot") if isinstance(base, dict) else None,
            "genres": genres,
            "realisateur": None,
            "acteurs": [],
            "note_tmdb": _rating_value(base) if isinstance(base, dict) else _rating_value(hit),
            "poster_path": img,
            "langue_originale": _spoken_lang(base) if isinstance(base, dict) else None,
            "annee": ay,
            "series_title": stitle,
            "series_key": f"imdb-{tid}" if tid else None,
        }

    season, episode = parsed
    show_q, year_hint = _show_query_from_filename(filename)
    hits = search_titles(show_q)
    hit = _pick_by_type_and_year(hits, "tvSeries", year_hint)
    if not hit:
        out = _empty_film_fields(guess)
        out["season_number"] = season
        out["episode_number"] = episode
        out["annee"] = year_hint
        return out

    show_id = str(hit["id"])
    ep_row = find_episode_on_show(show_id, season, episode)
    show_detail = get_title(show_id) or hit
    stitle = (
        show_detail.get("primaryTitle")
        if isinstance(show_detail, dict)
        else hit.get("primaryTitle")
    ) or show_q

    if not ep_row:
        return {
            "tmdb_id": None,
            "imdb_title_id": None,
            "titre": f"{stitle} S{season:02d}E{episode:02d}",
            "synopsis": show_detail.get("plot") if isinstance(show_detail, dict) else None,
            "genres": list(show_detail.get("genres") or []) if isinstance(show_detail, dict) else [],
            "realisateur": None,
            "acteurs": [],
            "note_tmdb": _rating_value(show_detail) if isinstance(show_detail, dict) else None,
            "poster_path": _primary_image_url(show_detail)
            if isinstance(show_detail, dict)
            else _primary_image_url(hit),
            "langue_originale": _spoken_lang(show_detail) if isinstance(show_detail, dict) else None,
            "annee": year_hint,
            "series_title": stitle,
            "series_key": f"imdb-{show_id}",
            "season_number": season,
            "episode_number": episode,
        }

    ep_id = ep_row.get("id")
    ep_detail = get_title(str(ep_id)) if ep_id else None
    ep_use = ep_detail or ep_row

    director = _directors_line(ep_use) if isinstance(ep_use, dict) else None
    cast: List[str] = []
    if ep_id:
        cast = fetch_credits_cast(str(ep_id))

    rd = ep_row.get("releaseDate") if isinstance(ep_row, dict) else None
    ay = None
    if isinstance(rd, dict) and rd.get("year") is not None:
        try:
            ay = int(rd["year"])
        except (TypeError, ValueError):
            ay = year_hint
    else:
        ay = year_hint

    ep_title = ep_use.get("primaryTitle") if isinstance(ep_use, dict) else ep_row.get("title")
    overview = ep_use.get("plot") if isinstance(ep_use, dict) else ep_row.get("plot")
    img = _primary_image_url(ep_use) if isinstance(ep_use, dict) else None
    if not img:
        img = _primary_image_url(ep_row) if isinstance(ep_row, dict) else None
    if not img and isinstance(show_detail, dict):
        img = _primary_image_url(show_detail)

    genres = list(show_detail.get("genres") or []) if isinstance(show_detail, dict) else []

    return {
        "tmdb_id": None,
        "imdb_title_id": str(ep_id) if ep_id else None,
        "titre": ep_title or f"{stitle} S{season:02d}E{episode:02d}",
        "titre_original": ep_use.get("originalTitle") if isinstance(ep_use, dict) else None,
        "synopsis": overview,
        "genres": genres,
        "realisateur": director,
        "acteurs": cast,
        "note_tmdb": _rating_value(ep_use) if isinstance(ep_use, dict) else None,
        "poster_path": img,
        "langue_originale": _spoken_lang(show_detail) if isinstance(show_detail, dict) else None,
        "annee": ay,
        "series_title": stitle,
        "series_key": f"imdb-{show_id}",
        "season_number": season,
        "episode_number": episode,
    }


def enrich_from_filename_imdb(filename: str, content_kind: ContentKind) -> Dict[str, Any]:
    if content_kind == ContentKind.series_episode:
        return enrich_series_episode_from_filename(filename)
    return enrich_movie_from_filename(filename)


def apply_imdb_title_metadata(film_row: Any) -> None:
    """Update a Film ORM instance from GET /titles/{imdb_title_id} (+ credits for cast)."""
    iid = getattr(film_row, "imdb_title_id", None)
    if not iid or not str(iid).startswith("tt"):
        return
    detail = get_title(str(iid))
    if not detail:
        return

    film_row.titre = detail.get("primaryTitle") or film_row.titre
    film_row.titre_original = detail.get("originalTitle") or film_row.titre_original
    film_row.synopsis = detail.get("plot") or film_row.synopsis
    g = detail.get("genres")
    if isinstance(g, list) and g:
        film_row.genres = g
    dline = _directors_line(detail)
    if dline:
        film_row.realisateur = dline
    rv = _rating_value(detail)
    if rv is not None:
        film_row.note_tmdb = rv
    img = _primary_image_url(detail)
    if img:
        film_row.poster_path = img
    if detail.get("startYear") is not None:
        try:
            film_row.annee = int(detail["startYear"])
        except (TypeError, ValueError):
            pass
    sl = _spoken_lang(detail)
    if sl:
        film_row.langue_originale = sl
    cast = fetch_credits_cast(str(iid))
    if cast:
        film_row.acteurs = cast
    else:
        sc = _stars_to_cast(detail)
        if sc:
            film_row.acteurs = sc
