"""Database engine and session factory."""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings
from db.models import Base

_settings = get_settings()

engine = create_engine(
    _settings.DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
)

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
                        poster_path VARCHAR(512),
                        note VARCHAR(512),
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
                    poster_path VARCHAR(512),
                    note VARCHAR(512),
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


def init_db() -> None:
    """Create all tables (development / first boot)."""
    Base.metadata.create_all(bind=engine)
    _ensure_films_trailer_columns()
    _ensure_films_imdb_title_id_column()
    _ensure_user_invite_column()
    _ensure_invitation_created_by_column()
    _ensure_series_season_meta_table()


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
