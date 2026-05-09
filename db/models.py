"""SQLAlchemy ORM models."""

import enum
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    admin = "admin"
    viewer = "viewer"


class FilmSource(str, enum.Enum):
    upload = "upload"
    torrent = "torrent"


class FilmTraitement(str, enum.Enum):
    direct = "direct"
    optimise = "optimise"
    transcode = "transcode"


class FilmStatut(str, enum.Enum):
    en_cours = "en_cours"
    disponible = "disponible"
    erreur = "erreur"


class ContentKind(str, enum.Enum):
    film = "film"
    series_episode = "series_episode"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    # Persist enum values ("admin"/"viewer") in PostgreSQL, not member names
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda x: [e.value for e in x]),
        default=UserRole.viewer,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    date_creation: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    derniere_connexion: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Viewer tastes: { "favorite_genres": ["Drame", "Action"] } for "surprise me" picks
    preferences: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship("User", back_populates="refresh_tokens")


class Film(Base):
    __tablename__ = "films"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    titre: Mapped[str] = mapped_column(String(512))
    titre_original: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    annee: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    synopsis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    genres: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    realisateur: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    acteurs: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    note_tmdb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    poster_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Single manual YouTube trailer [{ "key": "11chars", "name": "…", "type": "Trailer" }] merged with TMDB on film page
    trailers_manual: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # Cached TMDB /videos slice (at most one entry); refreshed by TTL or admin refresh-tmdb
    trailers_tmdb_cache: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    trailers_tmdb_cached_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    langue_originale: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    duree_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    resolution: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    codec_video: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    codec_audio: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    bitrate_kbps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    taille_octets: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    s3_key: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    s3_bucket: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    source: Mapped[FilmSource] = mapped_column(Enum(FilmSource), default=FilmSource.upload)
    traitement: Mapped[Optional[FilmTraitement]] = mapped_column(
        Enum(FilmTraitement), nullable=True
    )
    statut: Mapped[FilmStatut] = mapped_column(Enum(FilmStatut), default=FilmStatut.en_cours)
    erreur_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    date_ajout: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    url_streaming: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    # Optional progress for admin queue UI (0-100)
    pipeline_progress: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Live BitTorrent stats while aria2 downloads (seeders, leechers, bps, …)
    torrent_stats: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # Series: one row per episode; films keep content_kind=film and null series_* fields
    content_kind: Mapped[ContentKind] = mapped_column(
        Enum(ContentKind, values_callable=lambda x: [e.value for e in x]),
        default=ContentKind.film,
    )
    series_key: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, index=True)
    series_title: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    season_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_films_series_season_ep", "series_key", "season_number", "episode_number"),)


class InvitationCode(Base):
    __tablename__ = "invitation_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
