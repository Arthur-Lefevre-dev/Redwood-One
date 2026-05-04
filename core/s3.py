"""OVH S3-compatible storage helpers."""

import logging
import uuid
from pathlib import Path
from typing import Optional

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
