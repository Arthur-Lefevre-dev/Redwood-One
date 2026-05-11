"""Application configuration loaded from environment."""

from functools import lru_cache
from typing import List
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql://redwood:redwood@localhost:5432/redwood"

    # Celery broker: build URL with URL-encoded password so special chars (@ : # / …)
    # never break kombu's parser (avoid ValueError on broker URL).
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str = ""

    SECRET_KEY: str = "dev-secret-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    TMDB_API_KEY: str = ""
    # Store /movie/{id}/videos in DB; refresh after N days (0 = fetch every film page view when API key set).
    TMDB_TRAILERS_CACHE_DAYS: int = 7

    # Metadata enrichment: "tmdb" (default) or "imdbapi" (https://imdbapi.dev — no API key).
    METADATA_PROVIDER: str = "tmdb"
    IMDBAPI_BASE_URL: str = "https://api.imdbapi.dev"

    S3_ENDPOINT_URL: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET_NAME: str = "redwood-films"
    S3_REGION: str = "gra"

    MAX_UPLOAD_SIZE: int = 53_687_091_200  # 50 GB
    ALLOWED_ORIGINS: str = "http://localhost"

    # GPU: auto | amd | nvidia | intel | cpu — "amd" forces VAAPI if /dev/dri + ffmpeg vaapi (see docker-compose /dev/dri)
    REDWOOD_GPU_VENDOR: str = ""

    # Transcode pipeline (ffmpeg): target average video bitrate in kbit/s (e.g. 6000 ≈ 6 Mbit/s).
    # VBV maxrate/bufsize cap peaks; no -vf scale / no -r so resolution and frame rate follow the source.
    TRANSCODE_VIDEO_BITRATE_KBPS: int = 6000
    TRANSCODE_VIDEO_MAXRATE_KBPS: int = 7200
    TRANSCODE_VIDEO_BUFSIZE_KBPS: int = 12000

    # If True, POST /api/auth/register accepts users without an invite code (dev only).
    REGISTRATION_OPEN: bool = False

    # slowapi limit for POST /api/auth/login (e.g. "60/minute", "20/minute").
    AUTH_LOGIN_RATE_LIMIT: str = "60/minute"

    # Vast.ai GPU marketplace (optional — test / future remote transcode workers).
    # API key: https://cloud.vast.ai/manage-keys/
    VAST_API_KEY: str = ""
    VAST_API_BASE_URL: str = "https://console.vast.ai/api/v0"
    # Comma-separated GPU names for offer search — must match Vast bundle `gpu_name` strings.
    VAST_DEFAULT_GPU_NAMES: str = (
        "RTX 3060,RTX 4060,GTX 1070 Ti,RTX 3060 Ti,GTX 1080,RTX 3070,GTX 1060,RTX 3050,"
        "Titan Xp,GTX 1660 S,GTX 1080 Ti,GTX 1660"
    )
    # Bundles search: max total $/hour (dph_total).
    VAST_MAX_DPH_PER_HOUR: float = 0.058
    # Max Internet bandwidth price: $ per TB (applied as inet_*_cost $/GB lte = value/1024).
    VAST_MAX_BANDWIDTH_USD_PER_TB: float = 4.0
    # Remote transcode on Vast (Celery): Docker image on Vast (CUDA runtime + apt ffmpeg in onstart).
    VAST_TRANSCODE_DOCKER_IMAGE: str = "nvidia/cuda:12.3.1-runtime-ubuntu22.04"
    VAST_TRANSCODE_DISK_GB: int = 32
    VAST_TRANSCODE_URL_TTL_SEC: int = 7200
    VAST_TRANSCODE_POLL_INTERVAL_SEC: int = 15
    VAST_TRANSCODE_MAX_WAIT_SEC: int = 7200
    # Seconds to wait in onstart for /dev/nvidia0 (Vast can attach GPU nodes slightly after boot).
    VAST_TRANSCODE_GPU_DEVICE_WAIT_SEC: int = 90
    # If True, bundle search for auto-picked transcode uses num_gpus eq 1 only (reduces CDI gpu=N failures on some hosts).
    VAST_TRANSCODE_SINGLE_GPU_ONLY: bool = True

    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    ADMIN_EMAIL: str = "admin@redwoodplus.local"

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def redis_url(self) -> str:
        """Redis URL for Celery broker/backend (password always percent-encoded)."""
        host = (self.REDIS_HOST or "localhost").strip()
        if not self.REDIS_PASSWORD:
            return f"redis://{host}:{self.REDIS_PORT}/{self.REDIS_DB}"
        pw = quote(self.REDIS_PASSWORD, safe="")
        return f"redis://:{pw}@{host}:{self.REDIS_PORT}/{self.REDIS_DB}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
