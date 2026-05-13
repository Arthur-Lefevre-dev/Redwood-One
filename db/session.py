"""Database engine and session factory."""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings
from db.models import Base

_settings = get_settings()

_engine_kw: dict = {"pool_pre_ping": True, "echo": False}
# Default pool (5 + 10 overflow) starves under parallel admin polling + Celery DB use on one engine.
if str(_settings.DATABASE_URL).startswith("postgresql"):
    _engine_kw.update(
        pool_size=int(getattr(_settings, "SQLALCHEMY_POOL_SIZE", 20) or 20),
        max_overflow=int(getattr(_settings, "SQLALCHEMY_MAX_OVERFLOW", 40) or 40),
        pool_timeout=int(getattr(_settings, "SQLALCHEMY_POOL_TIMEOUT", 60) or 60),
        pool_recycle=int(getattr(_settings, "SQLALCHEMY_POOL_RECYCLE", 1800) or 1800),
    )

engine = create_engine(_settings.DATABASE_URL, **_engine_kw)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

logger = logging.getLogger(__name__)


def _ensure_films_trailer_columns() -> None:
    """
    create_all() does not add new columns to existing tables.
    Keep schema in sync when models gain optional JSON columns.
    """
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_manual JSONB"))
            conn.execute(text("ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_tmdb_cache JSONB"))
            conn.execute(text("ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_tmdb_cached_at TIMESTAMP"))
        logger.info("database schema: ensured films trailer columns (postgresql)")
        return
    insp = inspect(engine)
    if "films" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("films")}
    with engine.begin() as conn:
        if "trailers_manual" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN trailers_manual TEXT"))
        if "trailers_tmdb_cache" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN trailers_tmdb_cache TEXT"))
        if "trailers_tmdb_cached_at" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN trailers_tmdb_cached_at TEXT"))
    logger.info("database schema: ensured films trailer columns (sqlite)")


def _ensure_user_viewer_rank_column() -> None:
    """Viewer rank for monthly invitation quota (bronze/silver/gold/platinum)."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS viewer_rank VARCHAR(20)"))
            conn.execute(
                text(
                    "UPDATE users SET viewer_rank = 'bronze' "
                    "WHERE viewer_rank IS NULL AND role::text = 'viewer'"
                )
            )
        logger.info("database schema: ensured users.viewer_rank (postgresql)")
        return
    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "viewer_rank" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN viewer_rank VARCHAR(20)"))
        logger.info("database schema: added users.viewer_rank (sqlite)")
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET viewer_rank = 'bronze' "
                "WHERE viewer_rank IS NULL AND role = 'viewer'"
            )
        )


def _ensure_user_signup_origin_columns() -> None:
    """Track signup path: invite code id + channel (invite|open|admin)."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "registered_via_invite_code_id INTEGER "
                    "REFERENCES invitation_codes(id) ON DELETE SET NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_users_registered_via_invite_code_id "
                    "ON users(registered_via_invite_code_id)"
                )
            )
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_channel VARCHAR(20)"))
        logger.info("database schema: ensured users signup origin columns (postgresql)")
        return
    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    with engine.begin() as conn:
        if "registered_via_invite_code_id" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN registered_via_invite_code_id INTEGER"))
        if "signup_channel" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN signup_channel VARCHAR(20)"))
    logger.info("database schema: ensured users signup origin columns (sqlite)")


def _ensure_user_invite_column() -> None:
    """Add last_invite_at for monthly user-generated invitation codes."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_invite_at TIMESTAMP"))
        logger.info("database schema: ensured users.last_invite_at (postgresql)")
        return
    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "last_invite_at" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN last_invite_at DATETIME"))
    logger.info("database schema: ensured users.last_invite_at (sqlite)")


def _ensure_user_deactivated_at_column() -> None:
    """Timestamp when account was deactivated (admin); shown if user tries to log in."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP"))
        logger.info("database schema: ensured users.deactivated_at (postgresql)")
        return
    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "deactivated_at" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users ADD COLUMN deactivated_at DATETIME"))
    logger.info("database schema: ensured users.deactivated_at (sqlite)")


def _ensure_films_pipeline_celery_columns() -> None:
    """Track active Celery task id + kind for admin pipeline cancel."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE films ADD COLUMN IF NOT EXISTS pipeline_celery_task_id VARCHAR(64)")
            )
            conn.execute(
                text("ALTER TABLE films ADD COLUMN IF NOT EXISTS pipeline_celery_task_kind VARCHAR(32)")
            )
        logger.info("database schema: ensured films pipeline Celery columns (postgresql)")
        return
    insp = inspect(engine)
    if "films" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("films")}
    with engine.begin() as conn:
        if "pipeline_celery_task_id" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN pipeline_celery_task_id VARCHAR(64)"))
        if "pipeline_celery_task_kind" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN pipeline_celery_task_kind VARCHAR(32)"))
    logger.info("database schema: ensured films pipeline Celery columns (sqlite)")


def _ensure_films_vast_pending_columns() -> None:
    """Store S3 vast-test job token on film for POST /transcode/vast/retry after failures."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE films ADD COLUMN IF NOT EXISTS vast_pending_job_token VARCHAR(64)")
            )
            conn.execute(
                text("ALTER TABLE films ADD COLUMN IF NOT EXISTS vast_pending_input_ext VARCHAR(16)")
            )
        logger.info("database schema: ensured films.vast_pending_* (postgresql)")
        return
    insp = inspect(engine)
    if "films" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("films")}
    with engine.begin() as conn:
        if "vast_pending_job_token" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN vast_pending_job_token VARCHAR(64)"))
        if "vast_pending_input_ext" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN vast_pending_input_ext VARCHAR(16)"))
    logger.info("database schema: ensured films.vast_pending_* (sqlite)")


def _ensure_films_transcode_target_columns() -> None:
    """Torrent routing: local worker vs Vast GPU after download."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE films ADD COLUMN IF NOT EXISTS transcode_target VARCHAR(16)")
            )
            conn.execute(
                text("ALTER TABLE films ADD COLUMN IF NOT EXISTS vast_offer_id INTEGER")
            )
        logger.info("database schema: ensured films.transcode_target / vast_offer_id (postgresql)")
        return
    insp = inspect(engine)
    if "films" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("films")}
    with engine.begin() as conn:
        if "transcode_target" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN transcode_target VARCHAR(16)"))
        if "vast_offer_id" not in cols:
            conn.execute(text("ALTER TABLE films ADD COLUMN vast_offer_id INTEGER"))
    logger.info("database schema: ensured films.transcode_target / vast_offer_id (sqlite)")


def _ensure_films_imdb_title_id_column() -> None:
    """Optional IMDb id for imdbapi.dev enrichment."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE films ADD COLUMN IF NOT EXISTS imdb_title_id VARCHAR(16)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_films_imdb_title_id ON films(imdb_title_id)"))
        logger.info("database schema: ensured films.imdb_title_id (postgresql)")
        return
    insp = inspect(engine)
    if "films" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("films")}
    if "imdb_title_id" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE films ADD COLUMN imdb_title_id VARCHAR(16)"))
        logger.info("database schema: added films.imdb_title_id (sqlite)")


def _ensure_invitation_created_by_column() -> None:
    """Link member-generated invite codes to users for history UI."""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE invitation_codes ADD COLUMN IF NOT EXISTS "
                    "created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_invitation_codes_created_by_user_id "
                    "ON invitation_codes(created_by_user_id)"
                )
            )
        logger.info("database schema: ensured invitation_codes.created_by_user_id (postgresql)")
        return
    insp = inspect(engine)
    if "invitation_codes" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("invitation_codes")}
    if "created_by_user_id" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE invitation_codes ADD COLUMN created_by_user_id INTEGER"))
        logger.info("database schema: added invitation_codes.created_by_user_id (sqlite)")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_invitation_codes_created_by_user_id "
                "ON invitation_codes(created_by_user_id)"
            )
        )


def _ensure_series_season_meta_table() -> None:
    """Season poster/note per (series_key, season); belt-and-suspenders if create_all skipped."""
    insp = inspect(engine)
    if "series_season_meta" in insp.get_table_names():
        return
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE series_season_meta (
                        id SERIAL PRIMARY KEY,
                        series_key VARCHAR(160) NOT NULL,
                        season_number INTEGER NOT NULL,
                        poster_path VARCHAR(2048),
                        note VARCHAR(512),
                        synopsis TEXT,
                        CONSTRAINT uq_series_season_meta_key_sn UNIQUE (series_key, season_number)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_series_season_meta_series_key "
                    "ON series_season_meta (series_key)"
                )
            )
        logger.info("database schema: created series_season_meta (postgresql)")
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE series_season_meta (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    series_key VARCHAR(160) NOT NULL,
                    season_number INTEGER NOT NULL,
                    poster_path VARCHAR(2048),
                    note VARCHAR(512),
                    synopsis TEXT,
                    UNIQUE (series_key, season_number)
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_series_season_meta_series_key "
                "ON series_season_meta (series_key)"
            )
        )
    logger.info("database schema: created series_season_meta (sqlite)")


def _ensure_series_season_synopsis_column() -> None:
    """Add season synopsis for existing series_season_meta tables."""
    insp = inspect(engine)
    if "series_season_meta" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("series_season_meta")}
    if "synopsis" in cols:
        return
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE series_season_meta ADD COLUMN synopsis TEXT")
            )
        logger.info("database schema: added series_season_meta.synopsis (postgresql)")
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE series_season_meta ADD COLUMN synopsis TEXT"))
    logger.info("database schema: added series_season_meta.synopsis (sqlite)")


def _ensure_series_show_meta_table() -> None:
    """Global series page poster + hero text; belt-and-suspenders if create_all skipped."""
    insp = inspect(engine)
    if "series_show_meta" in insp.get_table_names():
        return
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE series_show_meta (
                        id SERIAL PRIMARY KEY,
                        series_key VARCHAR(160) NOT NULL UNIQUE,
                        poster_path VARCHAR(2048),
                        hero_text TEXT
                    )
                    """
                )
            )
        logger.info("database schema: created series_show_meta (postgresql)")
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE series_show_meta (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    series_key VARCHAR(160) NOT NULL UNIQUE,
                    poster_path VARCHAR(2048),
                    hero_text TEXT
                )
                """
            )
        )
    logger.info("database schema: created series_show_meta (sqlite)")


def _ensure_donation_settings_table() -> None:
    """Singleton crypto donation config (addresses, EUR goal, snapshot cache)."""
    insp = inspect(engine)
    if "donation_settings" in insp.get_table_names():
        return
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE donation_settings (
                        id INTEGER PRIMARY KEY,
                        goal_eur DOUBLE PRECISION,
                        address_btc VARCHAR(256),
                        address_polygon VARCHAR(256),
                        address_solana VARCHAR(256),
                        address_xrp VARCHAR(256),
                        address_tron VARCHAR(256),
                        campaign_start_utc TIMESTAMP,
                        campaign_end_utc TIMESTAMP,
                        recurrence VARCHAR(24) DEFAULT 'none',
                        snapshot_json JSONB,
                        updated_at TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc')
                    )
                    """
                )
            )
        logger.info("database schema: created donation_settings (postgresql)")
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE donation_settings (
                    id INTEGER NOT NULL PRIMARY KEY,
                    goal_eur REAL,
                    address_btc VARCHAR(256),
                    address_polygon VARCHAR(256),
                    address_solana VARCHAR(256),
                    address_xrp VARCHAR(256),
                    address_tron VARCHAR(256),
                    campaign_start_utc TIMESTAMP,
                    campaign_end_utc TIMESTAMP,
                    recurrence VARCHAR(24) DEFAULT 'none',
                    snapshot_json TEXT,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    logger.info("database schema: created donation_settings (sqlite)")


def _migrate_donation_xmr_to_xrp() -> None:
    """Rename legacy address_xmr column to address_xrp (Monero replaced by XRP)."""
    insp = inspect(engine)
    if "donation_settings" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("donation_settings")}
    if "address_xmr" in cols and "address_xrp" not in cols:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE donation_settings RENAME COLUMN address_xmr TO address_xrp")
            )
        logger.info("database schema: renamed donation_settings.address_xmr -> address_xrp")
        return
    if "address_xrp" not in cols:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE donation_settings ADD COLUMN address_xrp VARCHAR(256)")
            )
        logger.info("database schema: added donation_settings.address_xrp")


def _ensure_donation_settings_extended_columns() -> None:
    """Tron address + campaign window + recurrence on existing donation_settings."""
    insp = inspect(engine)
    if "donation_settings" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("donation_settings")}
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            if "address_tron" not in cols:
                conn.execute(
                    text("ALTER TABLE donation_settings ADD COLUMN address_tron VARCHAR(256)")
                )
            if "campaign_start_utc" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE donation_settings ADD COLUMN campaign_start_utc TIMESTAMP"
                    )
                )
            if "campaign_end_utc" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE donation_settings ADD COLUMN campaign_end_utc TIMESTAMP"
                    )
                )
            if "recurrence" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE donation_settings ADD COLUMN recurrence VARCHAR(24) "
                        "DEFAULT 'none'"
                    )
                )
        logger.info("database schema: ensured donation_settings tron/campaign columns (postgresql)")
        return
    with engine.begin() as conn:
        if "address_tron" not in cols:
            conn.execute(
                text("ALTER TABLE donation_settings ADD COLUMN address_tron VARCHAR(256)")
            )
        if "campaign_start_utc" not in cols:
            conn.execute(
                text("ALTER TABLE donation_settings ADD COLUMN campaign_start_utc DATETIME")
            )
        if "campaign_end_utc" not in cols:
            conn.execute(
                text("ALTER TABLE donation_settings ADD COLUMN campaign_end_utc DATETIME")
            )
        if "recurrence" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE donation_settings ADD COLUMN recurrence VARCHAR(24) "
                    "DEFAULT 'none'"
                )
            )
    logger.info("database schema: ensured donation_settings tron/campaign columns (sqlite)")


def _widen_series_meta_poster_columns() -> None:
    """Allow full TMDB/CDN URLs in poster_path (existing DBs may still be VARCHAR(512))."""
    if engine.dialect.name != "postgresql":
        return
    insp = inspect(engine)
    for table in ("series_show_meta", "series_season_meta"):
        if table not in insp.get_table_names():
            continue
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"ALTER TABLE {table} ALTER COLUMN poster_path TYPE VARCHAR(2048)"
                    )
                )
            logger.info("database schema: widened %s.poster_path to 2048", table)
        except Exception as exc:
            logger.warning(
                "database schema: could not widen %s.poster_path (%s)",
                table,
                exc,
            )


def init_db() -> None:
    """Create all tables (development / first boot)."""
    Base.metadata.create_all(bind=engine)
    _ensure_films_trailer_columns()
    _ensure_films_transcode_target_columns()
    _ensure_films_vast_pending_columns()
    _ensure_films_pipeline_celery_columns()
    _ensure_films_imdb_title_id_column()
    _ensure_user_invite_column()
    _ensure_user_deactivated_at_column()
    _ensure_user_signup_origin_columns()
    _ensure_user_viewer_rank_column()
    _ensure_invitation_created_by_column()
    _ensure_series_season_meta_table()
    _ensure_series_season_synopsis_column()
    _ensure_series_show_meta_table()
    _ensure_donation_settings_table()
    _migrate_donation_xmr_to_xrp()
    _ensure_donation_settings_extended_columns()
    _widen_series_meta_poster_columns()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for Celery tasks."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
