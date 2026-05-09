"""Parse YouTube trailer URLs / keys and merge with TMDB trailer lists."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# YouTube video IDs are 11 chars (alphanumeric, _, -)
_YT_ID = re.compile(r"^([a-zA-Z0-9_-]{11})$")
_YT_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?)(?:[^&\s]*&)*v=([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtu\.be/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube-nocookie\.com/embed/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})"),
]


def extract_youtube_video_id(text: str) -> Optional[str]:
    """Return 11-char YouTube key from a raw key or full URL."""
    t = (text or "").strip()
    if not t:
        return None
    m = _YT_ID.match(t)
    if m:
        return m.group(1)
    for p in _YT_PATTERNS:
        m = p.search(t)
        if m:
            return m.group(1)
    return None


def trailers_from_admin_lines(lines: List[str]) -> List[Dict[str, str]]:
    """
    Each line: optional title then URL or key, separated by | ; or a single URL/key.
    Examples:
      https://www.youtube.com/watch?v=xxxx
      Official trailer|https://youtu.be/xxxx
    """
    out: List[Dict[str, str]] = []
    for raw in lines:
        line = (raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name_part, url_part = line.split("|", 1)
            name = name_part.strip() or f"Bande-annonce ({len(out) + 1})"
            rest = url_part.strip()
        else:
            name = f"Bande-annonce ({len(out) + 1})"
            rest = line
        key = extract_youtube_video_id(rest)
        if key:
            out.append({"key": key, "name": name, "type": "Trailer"})
    return out


def trailers_from_json_column(raw: Any) -> List[Dict[str, str]]:
    """Normalize DB JSON (list of dicts with key) for API merge."""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for x in raw:
        if not isinstance(x, dict):
            continue
        key = x.get("key")
        if not key:
            continue
        key = str(key).strip()
        if len(key) != 11:
            extracted = extract_youtube_video_id(key)
            if not extracted:
                continue
            key = extracted
        out.append(
            {
                "key": key,
                "name": str(x.get("name") or "Bande-annonce").strip() or "Bande-annonce",
                "type": str(x.get("type") or "Trailer"),
            }
        )
    return out


def merge_trailer_lists(manual: List[Dict[str, Any]], tmdb: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Manual entries first; then TMDB; dedupe by YouTube key."""
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for item in manual + tmdb:
        k = item.get("key")
        if not k or k in seen:
            continue
        seen.add(str(k))
        out.append(
            {
                "key": str(k),
                "name": item.get("name") or "Bande-annonce",
                "type": item.get("type") or "Trailer",
            }
        )
    return out


def trailers_to_watch_urls(items: List[Dict[str, Any]]) -> List[str]:
    """Lines for admin textarea (one full watch URL per entry, optional Title|url)."""
    lines: List[str] = []
    for x in items:
        k = x.get("key")
        if not k:
            continue
        url = f"https://www.youtube.com/watch?v={k}"
        name = (x.get("name") or "").strip()
        lines.append(f"{name}|{url}" if name else url)
    return lines
