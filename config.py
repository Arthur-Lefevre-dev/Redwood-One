"""Application configuration loaded from environment."""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql://redwood:redwood@localhost:5432/redwood"
    REDIS_URL: str = "redis://localhost:6379/0"

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

    # If True, POST /api/auth/register accepts users without an invite code (dev only).
    REGISTRATION_OPEN: bool = False

    # slowapi limit for POST /api/auth/login (e.g. "60/minute", "20/minute").
    AUTH_LOGIN_RATE_LIMIT: str = "60/minute"

    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    ADMIN_EMAIL: str = "admin@redwoodplus.local"

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
