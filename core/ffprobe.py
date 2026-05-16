"""Video metadata via ffprobe (JSON)."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Codecs muxable as timed text (mov_text) in MP4 for browser TextTrack / Plyr.
_TEXT_SUBTITLE_CODECS = frozenset(
    {
        "subrip",
        "ass",
        "ssa",
        "webvtt",
        "mov_text",
        "srt",
        "text",
        "subviewer",
        "subviewer1",
    }
)


class FFprobeError(Exception):
    pass


def probe(path: str | Path) -> Dict[str, Any]:
    """Return ffprobe JSON root dict."""
    p = Path(path)
    if not p.is_file():
        raise FFprobeError(f"file not found: {path}")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(p),
    ]
    # Keep stderr separate: merging STDERR into STDOUT breaks JSON when ffprobe prints warnings.
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as e:
        raise FFprobeError("ffprobe not installed") from e
    if completed.returncode != 0:
        err = (completed.stderr or b"").decode(errors="replace").strip()
        tail = (completed.stdout or b"")[:4000].decode(errors="replace").strip()
        raise FFprobeError(
            f"ffprobe exited {completed.returncode}: {err or 'no stderr'}{'; stdout: ' + tail if tail else ''}"
        )
    out = completed.stdout or b""
    if out.startswith(b"\xef\xbb\xbf"):
        out = out[3:]
    text = out.decode(errors="replace").strip()
    if not text:
        raise FFprobeError("ffprobe returned empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        preview = text[:500].replace("\n", "\\n")
        raise FFprobeError(f"invalid ffprobe json: {e}; stdout_preview={preview!r}") from e


def summarize(data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract common fields for pipeline decisions."""
    fmt = data.get("format") or {}
    size = int(fmt.get("size") or 0)
    duration_s = float(fmt.get("duration") or 0)
    bitrate = int(fmt.get("bit_rate") or 0) // 1000 if fmt.get("bit_rate") else None

    video: Optional[Dict[str, Any]] = None
    audio: Optional[Dict[str, Any]] = None
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not video:
            video = s
        elif s.get("codec_type") == "audio" and not audio:
            audio = s

    if not video:
        raise FFprobeError("no video stream")

    width = video.get("width")
    height = video.get("height")
    res = f"{width}x{height}" if width and height else None
    vcodec = video.get("codec_name")
    acodec = audio.get("codec_name") if audio else None

    return {
        "size_bytes": size,
        "duration_sec": duration_s,
        "bitrate_kbps": bitrate,
        "codec_video": vcodec,
        "codec_audio": acodec,
        "resolution": res,
        "duration_min": int(round(duration_s / 60)) if duration_s else None,
    }


def probe_has_audio_stream(data: Dict[str, Any]) -> bool:
    """True if ffprobe JSON lists at least one audio stream."""
    return any(s.get("codec_type") == "audio" for s in (data.get("streams") or []))


_FRENCH_AUDIO_LANG = frozenset(
    {
        "fr",
        "fra",
        "fre",
        "french",
        "français",
        "francais",
    }
)

_FRENCH_AUDIO_TITLE_HINTS = (
    "french",
    "français",
    "francais",
    " vf",
    "vf ",
    "vff",
    "truefrench",
    "version française",
    "version francaise",
    " dub",
    "dubbed",
)


def _audio_streams_from_probe(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [s for s in (data.get("streams") or []) if s.get("codec_type") == "audio"]


def _normalize_audio_language(stream: Dict[str, Any]) -> str:
    tags = stream.get("tags") or {}
    raw = (tags.get("language") or tags.get("LANGUAGE") or "").strip().lower()
    if not raw:
        return ""
    return raw.replace("_", "-").split("-")[0]


def is_french_audio_stream(stream: Dict[str, Any]) -> bool:
    """True when ffprobe tags indicate French (language or title hints)."""
    lang = _normalize_audio_language(stream)
    if lang in _FRENCH_AUDIO_LANG or (lang and lang.startswith("fr")):
        return True
    title = ((stream.get("tags") or {}).get("title") or "").lower()
    title_compact = title.strip()
    if title_compact in ("vf", "vff", "vfi", "truefrench"):
        return True
    return any(hint in title for hint in _FRENCH_AUDIO_TITLE_HINTS)


def _audio_stream_preference_rank(stream: Dict[str, Any]) -> tuple:
    """Lower sorts first: prefer default/main French over commentary/AD."""
    tags = stream.get("tags") or {}
    title = (tags.get("title") or "").lower()
    penalty = 0
    if "commentary" in title or "comment" in title:
        penalty += 10
    if "descriptive" in title or "audio description" in title:
        penalty += 20
    disp = stream.get("disposition") or {}
    if disp.get("default") in (1, "1", True):
        penalty -= 5
    idx = stream.get("index")
    tie = int(idx) if isinstance(idx, int) else 9999
    return (penalty, tie)


def preferred_audio_stream_index_from_probe(data: Dict[str, Any]) -> Optional[int]:
    """
    Global ffprobe stream index for French audio when present, else first audio stream.
    Returns None when the file has no audio.
    """
    audio_streams = _audio_streams_from_probe(data)
    if not audio_streams:
        return None
    french = [s for s in audio_streams if is_french_audio_stream(s)]
    pick = (
        sorted(french, key=_audio_stream_preference_rank)[0]
        if french
        else audio_streams[0]
    )
    idx = pick.get("index")
    if isinstance(idx, int) and idx >= 0:
        return idx
    return None


def text_subtitle_stream_indices_from_probe(
    data: Dict[str, Any],
    *,
    max_tracks: int = 8,
) -> List[int]:
    """
    Global stream indices for subtitle streams that can be remuxed to MP4 mov_text.
    Skips bitmap / HDMV subs (e.g. hdmv_pgs_subtitle, dvd_subtitle).
    """
    out: List[int] = []
    for s in data.get("streams") or []:
        if s.get("codec_type") != "subtitle":
            continue
        name = (s.get("codec_name") or "").strip().lower()
        if name not in _TEXT_SUBTITLE_CODECS:
            continue
        idx = s.get("index")
        if isinstance(idx, int) and idx >= 0:
            out.append(idx)
        if len(out) >= max_tracks:
            break
    return out
