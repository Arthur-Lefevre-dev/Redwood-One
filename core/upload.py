"""Streaming upload validation and disk write."""

import logging
import os
import uuid
from pathlib import Path
from typing import Tuple

from fastapi import UploadFile

from config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v"}
CHUNK_SIZE = 1024 * 1024  # 1 MB
UPLOAD_DIR = Path("/tmp/redwood/uploads")


def ensure_upload_dir() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def validate_extension(filename: str) -> None:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported format: {ext}")


async def save_upload_stream(file: UploadFile) -> Tuple[str, int]:
    """
    Stream UploadFile to disk in 1MB chunks.
    Returns (absolute_path, total_bytes_written).
    """
    settings = get_settings()
    ensure_upload_dir()
    if not file.filename:
        raise ValueError("missing filename")
    validate_extension(file.filename)

    safe_name = f"{uuid.uuid4().hex}_{Path(file.filename).name}"
    dest = UPLOAD_DIR / safe_name
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
