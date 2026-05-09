"""Database engine and session factory."""

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


def _ensure_trailers_manual_column() -> None:
    """
    create_all() does not add new columns to existing tables.
    Keep schema in sync when models gain optional JSON columns.
    """
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE films ADD COLUMN IF NOT EXISTS trailers_manual JSONB")
            )
        return
    insp = inspect(engine)
    if "films" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("films")}
    if "trailers_manual" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE films ADD COLUMN trailers_manual TEXT"))


def init_db() -> None:
    """Create all tables (development / first boot)."""
    Base.metadata.create_all(bind=engine)
    _ensure_trailers_manual_column()


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
