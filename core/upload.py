"""Streaming upload validation and disk write."""

import logging
import os
import re
from pathlib import Path
from typing import Tuple

from fastapi import UploadFile

from config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}
CHUNK_SIZE = 1024 * 1024  # 1 MB
UPLOAD_DIR = Path("/tmp/redwood/uploads")
# Strip characters unsafe or awkward on common filesystems / shells.
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def validate_extension(filename: str) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported format: {ext}")


def sanitize_upload_basename(filename: str) -> str:
    """Keep original filename (basename only); remove path tricks and unsafe chars."""
    base = Path(filename).name.strip()
    if not base or base in (".", ".."):
        raise ValueError("invalid filename")
    base = _UNSAFE_NAME.sub("_", base)
    base = "".join(c for c in base if ord(c) >= 32)
    if len(base) > 240:
        stem, suf = Path(base).stem[:200], Path(base).suffix
        base = stem + suf
    if not Path(base).suffix:
        raise ValueError("missing extension")
    validate_extension(base)
    return base


def _unique_dest_path(basename: str) -> Path:
    """If basename exists, use 'name (1).ext', 'name (2).ext', …"""
    dest = UPLOAD_DIR / basename
    if not dest.exists():
        return dest
    stem, suf = Path(basename).stem, Path(basename).suffix
    n = 1
    while True:
        cand = UPLOAD_DIR / f"{stem} ({n}){suf}"
        if not cand.exists():
            return cand
        n += 1


async def save_upload_stream(file: UploadFile) -> Tuple[str, int]:
    """
    Stream UploadFile to disk in 1MB chunks.
    Returns (absolute_path, total_bytes_written).
    Preserves the original filename (sanitized); picks a numeric suffix on collision.
    """
    settings = get_settings()
    ensure_upload_dir()
    if not file.filename:
        raise ValueError("missing filename")
    validate_extension(file.filename)

    safe_name = sanitize_upload_basename(file.filename)
    dest = _unique_dest_path(safe_name)
    total = 0
    logger.info("upload: start %s -> %s", file.filename, dest)

    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > settings.MAX_UPLOAD_SIZE:
                out.close()
                try:
                    os.remove(dest)
                except OSError:
                    pass
                raise ValueError("file exceeds MAX_UPLOAD_SIZE")
            out.write(chunk)

    return str(dest.resolve()), total
