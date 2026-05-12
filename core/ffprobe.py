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
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
    except subprocess.CalledProcessError as e:
        raise FFprobeError(e.output.decode(errors="replace") if e.output else str(e)) from e
    except FileNotFoundError as e:
        raise FFprobeError("ffprobe not installed") from e
    try:
        return json.loads(out.decode())
    except json.JSONDecodeError as e:
        raise FFprobeError(f"invalid ffprobe json: {e}") from e


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
