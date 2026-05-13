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
    # SQLAlchemy QueuePool (PostgreSQL only; SQLite uses defaults). Raise if admin UI + workers exhaust pool.
    SQLALCHEMY_POOL_SIZE: int = 20
    SQLALCHEMY_MAX_OVERFLOW: int = 40
    SQLALCHEMY_POOL_TIMEOUT: int = 60
    SQLALCHEMY_POOL_RECYCLE: int = 1800

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
    TRANSCODE_VIDEO_BITRATE_KBPS: int = 4000
    TRANSCODE_VIDEO_MAXRATE_KBPS: int = 6000
    TRANSCODE_VIDEO_BUFSIZE_KBPS: int = 12000
    # AAC audio bitrate (kbit/s) for transcoded MP4 (local + Vast onstart).
    TRANSCODE_AUDIO_BITRATE_KBPS: int = 160

    # If True, POST /api/auth/register accepts users without an invite code (dev only).
    REGISTRATION_OPEN: bool = False

    # slowapi limit for POST /api/auth/login (e.g. "60/minute", "20/minute").
    AUTH_LOGIN_RATE_LIMIT: str = "60/minute"

    # Vast.ai GPU marketplace (optional — test / future remote transcode workers).
    # API key: https://cloud.vast.ai/manage-keys/
    VAST_API_KEY: str = ""
    VAST_API_BASE_URL: str = "https://console.vast.ai/api/v0"
    # Comma-separated GPU names for offer search / auto-pick — must match Vast bundle `gpu_name` strings.
    VAST_DEFAULT_GPU_NAMES: str = (
        "GTX 1070 Ti,GTX 1080,GTX 1080 Ti,Titan Xp,RTX 2060,RTX 2060 SUPER,RTX 2060S,RTX 2070,RTX 2070 SUPER,RTX 2080,RTX 2080 SUPER,RTX 2080 Ti,TITAN RTX,RTX 3060,RTX 3060 Ti,RTX 3070,RTX 3070 Ti,RTX 3080,RTX 3080 Ti,RTX 3090,RTX 3090 Ti,RTX 4060,RTX 4060 Ti,RTX 4070,RTX 4070 SUPER,RTX 4070 Ti,RTX 4070 Ti SUPER,RTX 4080,RTX 4080 SUPER,RTX 4090,RTX 5050,RTX 5060,RTX 5060 Ti,RTX 5070,RTX 5070 Ti,RTX 5080,RTX 5090"
    )
    # Secondary tier (NVENC still usable); excluded from default search. See GET /api/admin/vast/offers?gpu_tier=…
    VAST_USABLE_GPU_NAMES: str = "GTX 1660,GTX 1660 SUPER,GTX 1660 S,GTX 1660 Ti,RTX 3050,GTX 1060"
    # Bundles search: max total $/hour (dph_total).
    VAST_MAX_DPH_PER_HOUR: float = 0.08
    # Max Internet bandwidth price: $ per TB (applied as inet_*_cost $/GB lte = value/1024).
    VAST_MAX_BANDWIDTH_USD_PER_TB: float = 4.0
    # Bundles search: minimum host internet speeds (Mb/s per Vast API — see CLI docs inet_down / inet_up).
    # Set to 0 to disable that bound. Used for auto-pick and GET /api/admin/vast/offers.
    VAST_MIN_INET_DOWN_MBPS: float = 120.0
    VAST_MIN_INET_UP_MBPS: float = 120.0
    # Comma-separated ISO 3166-1 alpha-2 codes excluded from Vast bundle search (geolocation notin + response filter).
    # Default excludes China (CN). Empty string = do not exclude any country.
    VAST_EXCLUDE_GEOLOCATION_CODES: str = "CN"
    # Comma-separated bundle machine_id / host_id to never auto-pick (buggy hosts). Dashboard shows these;
    # instance contract id changes each rental — use machine/host to block the physical host.
    VAST_SKIP_MACHINE_IDS: str = ""
    VAST_SKIP_HOST_IDS: str = ""
    # Remote transcode on Vast (Celery): Docker image on Vast (CUDA runtime + apt ffmpeg in onstart).
    VAST_TRANSCODE_DOCKER_IMAGE: str = "nvidia/cuda:12.3.1-runtime-ubuntu22.04"
    # Mount all driver libs (incl. NVENC); "compute" alone often breaks h264_nvenc on Vast.
    VAST_TRANSCODE_NVIDIA_DRIVER_CAPABILITIES: str = "all"
    # Visible GPU index(es) for the Vast container. Use "0" for typical 1×GPU contracts — "all" can make
    # Docker/CDI try to inject every host GPU (e.g. gpu=3) and fail with "unresolvable CDI devices".
    VAST_TRANSCODE_NVIDIA_VISIBLE_DEVICES: str = "0"
    # If non-empty, onstart downloads this BtbN FFmpeg tarball (NVENC-friendly) before encoding.
    # Empty skips download and uses apt ffmpeg only (NVENC often fails vs host driver on Vast despite GPU).
    VAST_TRANSCODE_BTBH_FFMPEG_URL: str = (
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
        "ffmpeg-master-latest-linux64-gpl.tar.xz"
    )
    # Parallel download of source into the Vast instance (aria2c; S3 supports Range). 1–32.
    VAST_TRANSCODE_INPUT_ARIA2_CONN: int = 16
    VAST_TRANSCODE_INPUT_ARIA2_SPLIT: int = 16
    VAST_TRANSCODE_DISK_GB: int = 32
    VAST_TRANSCODE_URL_TTL_SEC: int = 7200
    VAST_TRANSCODE_POLL_INTERVAL_SEC: int = 15
    VAST_TRANSCODE_MAX_WAIT_SEC: int = 7200
    # Seconds to wait in onstart for /dev/nvidia0 (Vast can attach GPU nodes slightly after boot).
    VAST_TRANSCODE_GPU_DEVICE_WAIT_SEC: int = 90
    # If an instance never exposes /dev/nvidia0, destroy it and try another offer (auto-pick only), up to this many rounds.
    VAST_TRANSCODE_NO_GPU_MAX_RETRIES: int = 6
    # If True, bundle search for auto-picked transcode uses num_gpus eq 1 only (reduces CDI gpu=N failures on some hosts).
    VAST_TRANSCODE_SINGLE_GPU_ONLY: bool = True
    # If True, POST /bundles/ requests verified eq true and auto-pick skips offers with verified=false.
    # Set to false to include Vast "Unverified Machines" in search and remote transcode auto-pick.
    VAST_BUNDLES_VERIFIED_ONLY: bool = True

    # Billing / cost estimates for admin dashboard (not legal invoices).
    # OVH-style object storage: € HT per GiB per hour (example default; override in .env).
    BILLING_STORAGE_EUR_PER_GIB_HOUR_HT: float = 0.00000972
    # Converts Vast.ai upper-bound USD estimates to EUR for display only.
    BILLING_USD_TO_EUR: float = 0.92
    # Optional flat estimate for local worker transcode (€ per minute of output), 0 = hide local line.
    BILLING_LOCAL_TRANSCODE_EUR_PER_MINUTE: float = 0.0

    # Watch pages: optional Coinzilla tag (e.g. popunder). Paste the async script URL from the publisher dashboard.
    WATCH_ADS_COINZILLA_ENABLED: bool = False
    WATCH_ADS_COINZILLA_SCRIPT_SRC: str = ""
    # If your snippet uses data-zone (or similar), set the zone id here; otherwise leave empty.
    WATCH_ADS_COINZILLA_ZONE_ID: str = ""

    TORRENT_AUTO_RETRY_ENABLED: bool = True
    TORRENT_AUTO_RETRY_MAX: int = 5

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
