"""TMDB API client (sync httpx)."""

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


def _clean_title_guess(filename: str) -> str:
    base = re.sub(r"\.[^.]+$", "", filename)
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


def enrich_from_filename(filename: str) -> Dict[str, Any]:
    """Return fields to merge into Film model."""
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
