"""OVH S3-compatible storage helpers."""

import logging
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def _s3_can_mutate_objects() -> bool:
    """True when bucket + credentials are set (endpoint optional: default AWS endpoint works)."""
    s = get_settings()
    return bool(s.S3_BUCKET_NAME and s.S3_ACCESS_KEY and s.S3_SECRET_KEY)


def delete_film_prefix(film_id: int, known_s3_key: Optional[str] = None) -> int:
    """
    Delete objects for this film: everything under films/{film_id}/, plus known_s3_key if set
    (covers DB-only key when ListBucket is restricted or listing returns nothing).
    Returns the number of keys removed. No-op when S3 credentials are not configured.
    Raises on API errors when deletion is attempted.
    """
    s = get_settings()
    if not _s3_can_mutate_objects():
        logger.warning(
            "s3: skip delete for film_id=%s (set S3_BUCKET_NAME, S3_ACCESS_KEY, S3_SECRET_KEY; "
            "use S3_ENDPOINT_URL for OVH/MinIO)",
            film_id,
        )
        return 0
    client = get_s3_client()
    prefix = f"films/{int(film_id)}/"
    seen: set[str] = set()
    keys: List[str] = []
    paginator = client.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=s.S3_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents") or []:
                k = obj.get("Key")
                if k and k not in seen:
                    seen.add(k)
                    keys.append(k)
    except Exception as e:
        logger.warning("s3: list_objects prefix=%s failed (%s); will still try known_s3_key", prefix, e)
    extra = (known_s3_key or "").strip()
    if extra and extra not in seen:
        keys.append(extra)
        seen.add(extra)
    if not keys:
        logger.info("s3: no keys to delete for film_id=%s (prefix %s, known_s3_key=%s)", film_id, prefix, extra or "—")
        return 0
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        resp = client.delete_objects(
            Bucket=s.S3_BUCKET_NAME,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        errs = resp.get("Errors") or []
        if errs:
            first = errs[0]
            msg = first.get("Message") or str(first)
            code = first.get("Code") or ""
            raise RuntimeError(f"S3 delete_objects error ({code}): {msg}")
        deleted += len(batch)
    logger.info("s3: deleted %s object(s) for film_id=%s (prefix %s)", deleted, film_id, prefix)
    return deleted
