"""OVH S3-compatible storage helpers."""

import logging
import re
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple

import boto3
from botocore.client import BaseClient

from config import get_settings

logger = logging.getLogger(__name__)

_client: Optional[BaseClient] = None


def get_s3_client():
    global _client
    if _client is None:
        s = get_settings()
        _client = boto3.client(
            "s3",
            endpoint_url=s.S3_ENDPOINT_URL or None,
            aws_access_key_id=s.S3_ACCESS_KEY or None,
            aws_secret_access_key=s.S3_SECRET_KEY or None,
            region_name=s.S3_REGION,
        )
    return _client


def build_object_key(film_id: int, filename: str) -> str:
    ext = Path(filename).suffix.lower() or ".mp4"
    return f"films/{film_id}/{uuid.uuid4().hex}{ext}"


def upload_file(local_path: str, key: str) -> None:
    s = get_settings()
    if not s.S3_BUCKET_NAME or not s.S3_ENDPOINT_URL:
        raise RuntimeError("S3 not configured")
    client = get_s3_client()
    client.upload_file(local_path, s.S3_BUCKET_NAME, key)
    logger.info("s3: uploaded %s -> s3://%s/%s", local_path, s.S3_BUCKET_NAME, key)


def presigned_stream_url(key: str, expires: int = 3600) -> str:
    s = get_settings()
    client = get_s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": s.S3_BUCKET_NAME, "Key": key},
        ExpiresIn=expires,
    )


_FILM_KEY = re.compile(r"^films/(\d+)/([^/]+)$", re.I)
_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".wmv"}


def list_film_objects_by_id() -> Dict[int, str]:
    """
    List video objects under films/{id}/... in the bucket.
    Returns film_id -> object key (latest LastModified if duplicates).
    """
    s = get_settings()
    if not s.S3_BUCKET_NAME or not s.S3_ENDPOINT_URL:
        raise RuntimeError("S3 not configured")
    client = get_s3_client()
    best: Dict[int, Tuple[float, str]] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=s.S3_BUCKET_NAME, Prefix="films/"):
        for obj in page.get("Contents") or []:
            key = obj.get("Key") or ""
            m = _FILM_KEY.match(key)
            if not m:
                continue
            ext = Path(m.group(2)).suffix.lower()
            if ext not in _VIDEO_EXT:
                continue
            fid = int(m.group(1))
            ts = 0.0
            if obj.get("LastModified"):
                ts = obj["LastModified"].timestamp()
            prev = best.get(fid)
            if prev is None or ts >= prev[0]:
                best[fid] = (ts, key)
    return {fid: pair[1] for fid, pair in best.items()}
