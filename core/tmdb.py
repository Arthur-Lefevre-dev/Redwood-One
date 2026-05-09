"""TMDB API client (sync httpx)."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import get_settings
from db.models import ContentKind

logger = logging.getLogger(__name__)

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


def _clean_title_guess(filename: str) -> str:
    base = re.sub(r"\.[^.]+$", "", filename)
    # Legacy uploads used a 32-char hex prefix on disk; strip so TMDB/title guess stays clean.
    base = re.sub(r"^[a-f0-9]{32}[_\s.-]+", "", base, flags=re.I)
    base = re.sub(r"[\._]", " ", base)
    base = re.sub(
        r"\b(19|20)\d{2}\b.*$",
        "",
        base,
    )
    base = re.sub(
        r"\b(720p|1080p|2160p|4k|bluray|brrip|web-?dl|hdr|x264|x265|hevc|h265|h264)\b.*$",
        "",
        base,
        flags=re.I,
    )
    return base.strip() or filename


def search_tv(query: str, first_air_date_year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """First TV show result from /search/tv."""
    settings = get_settings()
    if not settings.TMDB_API_KEY:
        logger.warning("tmdb: TMDB_API_KEY missing, skipping TV search")
        return None
    params: Dict[str, Any] = {"api_key": settings.TMDB_API_KEY, "query": query.strip()}
    if first_air_date_year:
        params["first_air_date_year"] = first_air_date_year
    url = "https://api.themoviedb.org/3/search/tv"
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            results = r.json().get("results") or []
            return results[0] if results else None
    except Exception as e:
        logger.exception("tmdb TV search failed: %s", e)
        return None


def tv_series_details(tv_id: int) -> Optional[Dict[str, Any]]:
    settings = get_settings()
    if not settings.TMDB_API_KEY:
        return None
    url = f"https://api.themoviedb.org/3/tv/{int(tv_id)}"
    params = {"api_key": settings.TMDB_API_KEY, "append_to_response": "credits"}
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.exception("tmdb TV details failed: %s", e)
        return None


def tv_season_episode(tv_id: int, season_number: int, episode_number: int) -> Optional[Dict[str, Any]]:
    """Single episode JSON; None if 404."""
    settings = get_settings()
    if not settings.TMDB_API_KEY:
        return None
    url = f"https://api.themoviedb.org/3/tv/{int(tv_id)}/season/{int(season_number)}/episode/{int(episode_number)}"
    params = {"api_key": settings.TMDB_API_KEY, "append_to_response": "credits,guest_stars"}
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, params=params)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.exception("tmdb TV episode failed: %s", e)
        return None


def parse_tv_season_episode(filename: str) -> Optional[Tuple[int, int]]:
    """Extract (season, episode) from typical release filenames (S01E02, 1x02, …)."""
    name = Path(filename).name
    patterns = [
        r"(?i)\bS(\d{1,4})[\s._-]*E(\d{1,4})\b",
        r"(?i)\b(\d{1,2})[\s._-]*x[\s._-]*(\d{1,4})\b",
        r"(?i)\bSeason[\s._-]*(\d{1,4})[\s._-]*(?:Episode|Ep)[\s._-]*(\d{1,4})\b",
    ]
    for pat in patterns:
        m = re.search(pat, name)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _show_query_from_filename(filename: str) -> Tuple[str, Optional[int]]:
    """
    Strip episode markers and junk so the remainder is a good /search/tv query.
    Returns (query, optional first_air_date_year).
    """
    base = Path(filename).name
    base = re.sub(r"\.[^.]+$", "", base)
    base = re.sub(r"^[a-f0-9]{32}[_\s.-]+", "", base, flags=re.I)
    # Drop from SxxExx (or 1x02) onward — episode title in filename would be lost; show name is before.
    base = re.sub(r"(?i)\bS\d{1,4}[\s._-]*E\d{1,4}\b.*$", "", base)
    base = re.sub(r"(?i)\b\d{1,2}[\s._-]*x[\s._-]*\d{1,4}\b.*$", "", base)
    base = re.sub(r"(?i)\bSeason[\s._-]*\d{1,4}[\s._-]*(?:Episode|Ep)[\s._-]*\d{1,4}\b.*$", "", base)
    base = re.sub(r"[\._]", " ", base)
    base = re.sub(
        r"\b(720p|1080p|2160p|4k|bluray|brrip|web-?dl|hdr|x264|x265|hevc|h265|h264|aac|ddp?5\.1)\b.*$",
        "",
        base,
        flags=re.I,
    )
    base = base.strip()
    year_m = re.search(r"\b(19|20)\d{2}\b", base)
    year = int(year_m.group(0)) if year_m else None
    if year:
        base = re.sub(r"\b(19|20)\d{2}\b", "", base).strip()
    base = re.sub(r"\s+", " ", base).strip()
    return base or Path(filename).stem, year


def enrich_tv_episode_from_filename(filename: str) -> Dict[str, Any]:
    """
    Enrich a series episode using /search/tv + season/episode endpoint.
    Stores TV series id in tmdb_id (not movie id, not episode-only id) so refresh works with season/episode.
    """
    parsed = parse_tv_season_episode(filename)
    guess = _clean_title_guess(filename)
    settings = get_settings()
    if not settings.TMDB_API_KEY:
        out = {
            "titre": guess,
            "tmdb_id": None,
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
        if parsed:
            out["season_number"], out["episode_number"] = parsed
        return out

    if not parsed:
        hit = search_tv(guess)
        if not hit:
            return {
                "titre": guess,
                "tmdb_id": None,
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
        tid = int(hit["id"])
        detail = tv_series_details(tid) or hit
        genres = [g.get("name") for g in (detail.get("genres") or []) if g.get("name")]
        stitle = detail.get("name") or hit.get("name") or guess
        rel = detail.get("first_air_date") or ""
        ay = int(rel[:4]) if rel and len(rel) >= 4 else None
        return {
            "tmdb_id": tid,
            "titre": guess,
            "titre_original": None,
            "synopsis": detail.get("overview"),
            "genres": genres,
            "realisateur": None,
            "acteurs": [],
            "note_tmdb": detail.get("vote_average"),
            "poster_path": detail.get("poster_path"),
            "langue_originale": detail.get("original_language"),
            "annee": ay,
            "series_title": stitle,
            "series_key": f"tv-{tid}",
        }

    season, episode = parsed
    show_q, year_hint = _show_query_from_filename(filename)
    hit = search_tv(show_q, first_air_date_year=year_hint)
    if not hit:
        return {
            "titre": guess,
            "tmdb_id": None,
            "synopsis": None,
            "genres": [],
            "realisateur": None,
            "acteurs": [],
            "note_tmdb": None,
            "poster_path": None,
            "langue_originale": None,
            "annee": year_hint,
            "series_title": None,
            "series_key": None,
            "season_number": season,
            "episode_number": episode,
        }

    tv_id = int(hit["id"])
    ep = tv_season_episode(tv_id, season, episode)
    show = tv_series_details(tv_id) or hit

    genres = [g.get("name") for g in (show.get("genres") or []) if g.get("name")]
    stitle = show.get("name") or hit.get("name") or show_q

    director = None
    if ep and ep.get("crew"):
        for c in ep["crew"]:
            if c.get("job") == "Director":
                director = c.get("name")
                break

    cast: List[str] = []
    if ep and ep.get("guest_stars"):
        cast = [a.get("name") for a in ep["guest_stars"][:12] if a.get("name")]
    if ep and len(cast) < 4 and show.get("credits", {}).get("cast"):
        for a in show["credits"]["cast"][:8]:
            n = a.get("name")
            if n and n not in cast:
                cast.append(n)

    ep_title = (ep.get("name") if ep else None) or f"{stitle} S{season:02d}E{episode:02d}"
    overview = (ep.get("overview") if ep else None) or show.get("overview")
    still = ep.get("still_path") if ep else None
    poster = still or show.get("poster_path")
    vote = ep.get("vote_average") if ep is not None else show.get("vote_average")
    ad = (ep.get("air_date") if ep else None) or ""
    ay = int(ad[:4]) if ad and len(ad) >= 4 else year_hint

    return {
        "tmdb_id": tv_id,
        "titre": ep_title,
        "titre_original": ep.get("name") if ep else None,
        "synopsis": overview,
        "genres": genres,
        "realisateur": director,
        "acteurs": cast,
        "note_tmdb": vote,
        "poster_path": poster,
        "langue_originale": show.get("original_language"),
        "annee": ay,
        "series_title": stitle,
        "series_key": f"tv-{tv_id}",
        "season_number": season,
        "episode_number": episode,
    }


def search_movie(query: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    settings = get_settings()
    if not settings.TMDB_API_KEY:
        logger.warning("tmdb: TMDB_API_KEY missing, skipping enrichment")
        return None
    params: Dict[str, Any] = {"api_key": settings.TMDB_API_KEY, "query": query}
    if year:
        params["year"] = year
    url = "https://api.themoviedb.org/3/search/movie"
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            results = r.json().get("results") or []
            return results[0] if results else None
    except Exception as e:
        logger.exception("tmdb search failed: %s", e)
        return None


def movie_details(tmdb_id: int) -> Optional[Dict[str, Any]]:
    settings = get_settings()
    if not settings.TMDB_API_KEY:
        return None
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}"
    params = {"api_key": settings.TMDB_API_KEY, "append_to_response": "credits"}
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.exception("tmdb details failed: %s", e)
        return None


def movie_trailers_youtube(tmdb_id: int, limit: int = 6) -> List[Dict[str, Any]]:
    """YouTube embed keys for trailers/teasers (TMDB /movie/{id}/videos)."""
    settings = get_settings()
    if not settings.TMDB_API_KEY:
        return []
    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/videos"
    params = {"api_key": settings.TMDB_API_KEY}
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            raw = r.json().get("results") or []
    except Exception as e:
        logger.exception("tmdb videos failed: %s", e)
        return []
    # Any YouTube key works in our iframe embed; prefer trailers/teasers first.
    candidates = [v for v in raw if (v.get("site") or "").lower() == "youtube" and v.get("key")]

    def sort_key(v: Dict[str, Any]) -> tuple:
        t = v.get("type") or ""
        order = {
            "Trailer": 0,
            "Teaser": 1,
            "Clip": 2,
            "Featurette": 3,
            "Behind the Scenes": 4,
            "Blooper": 5,
            "Opening Credit": 6,
        }.get(t, 7)
        official = 0 if v.get("official") else 1
        return (order, official, v.get("name") or "")

    candidates.sort(key=sort_key)
    out: List[Dict[str, Any]] = []
    lim = max(0, min(limit, 24))
    for v in candidates[:lim]:
        out.append(
            {
                "key": v["key"],
                "name": (v.get("name") or "Bande-annonce").strip(),
                "type": v.get("type") or "Trailer",
            }
        )
    return out


def enrich_from_filename(
    filename: str,
    content_kind: ContentKind = ContentKind.film,
) -> Dict[str, Any]:
    """Return fields to merge into Film model (movie or TV episode)."""
    prov = (get_settings().METADATA_PROVIDER or "tmdb").strip().lower()
    if prov == "imdbapi":
        from core.imdbapi import enrich_from_filename_imdb

        return enrich_from_filename_imdb(filename, content_kind)

    if content_kind == ContentKind.series_episode:
        return enrich_tv_episode_from_filename(filename)

    guess = _clean_title_guess(filename)
    m = re.search(r"\b(19|20)\d{2}\b", filename)
    year = int(m.group(0)) if m else None
    hit = search_movie(guess, year)
    if not hit:
        return {
            "titre": guess,
            "tmdb_id": None,
            "synopsis": None,
            "genres": [],
            "realisateur": None,
            "acteurs": [],
            "note_tmdb": None,
            "poster_path": None,
            "langue_originale": None,
            "annee": year,
        }

    tid = hit.get("id")
    detail = movie_details(int(tid)) if tid else None
    base = detail or hit

    genres: List[str] = []
    if detail and detail.get("genres"):
        genres = [g.get("name") for g in detail["genres"] if g.get("name")]

    director = None
    if detail and detail.get("credits", {}).get("crew"):
        for c in detail["credits"]["crew"]:
            if c.get("job") == "Director":
                director = c.get("name")
                break

    cast: List[str] = []
    if detail and detail.get("credits", {}).get("cast"):
        cast = [a.get("name") for a in detail["credits"]["cast"][:12] if a.get("name")]

    rel = base.get("release_date") or ""
    ay = int(rel[:4]) if rel and len(rel) >= 4 else year

    return {
        "tmdb_id": tid,
        "titre": base.get("title") or guess,
        "titre_original": base.get("original_title"),
        "synopsis": base.get("overview"),
        "genres": genres,
        "realisateur": director,
        "acteurs": cast,
        "note_tmdb": base.get("vote_average"),
        "poster_path": base.get("poster_path"),
        "langue_originale": base.get("original_language"),
        "annee": ay,
    }
