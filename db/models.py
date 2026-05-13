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
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    admin = "admin"
    viewer = "viewer"


class ViewerRank(str, enum.Enum):
    """Viewer tier: higher rank = more member invitations per UTC month."""

    bronze = "bronze"
    silver = "silver"
    gold = "gold"
    platinum = "platinum"


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


class SupportTicketCategory(str, enum.Enum):
    """Viewer support ticket type (stored as value string in DB)."""

    request_content = "request_content"
    bug = "bug"
    suggestion = "suggestion"
    account = "account"
    other = "other"


class SupportTicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


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
    # Set when an admin deactivates the account (shown on login attempt).
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    date_creation: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    derniere_connexion: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Last time this user generated a guest invite code (one per calendar month, UTC).
    last_invite_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Viewer tastes: { "favorite_genres": ["Drame", "Action"] } for "surprise me" picks
    preferences: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # Invitation quota tier (viewers): bronze|silver|gold|platinum — stored as string for simple migrations.
    viewer_rank: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default=None)
    # How the account was created: invite (registration with admin/member code), open (REGISTRATION_OPEN), admin (panel).
    signup_channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Invitation code row used at self-registration (null for admin-created or open registration without code).
    registered_via_invite_code_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("invitation_codes.id", ondelete="SET NULL"), nullable=True, index=True
    )

    refresh_tokens: Mapped[List["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    support_tickets: Mapped[List["SupportTicket"]] = relationship(
        "SupportTicket",
        back_populates="user",
        foreign_keys="SupportTicket.user_id",
        cascade="all, delete-orphan",
    )
    support_ticket_messages: Mapped[List["SupportTicketMessage"]] = relationship(
        "SupportTicketMessage",
        back_populates="author",
        foreign_keys="SupportTicketMessage.author_id",
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
    # IMDb title id (e.g. tt1375666) when using METADATA_PROVIDER=imdbapi; episode or movie id.
    imdb_title_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
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
    # Persisted torrent source for Celery retries (magnet URI or path to .torrent blob under /tmp/redwood).
    torrent_magnet_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    torrent_blob_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    torrent_auto_retry_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # After torrent download: "local" = worker ffmpeg pipeline; "vast" = S3 + Vast GPU transcode then finalize to library.
    transcode_target: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # When transcode_target is vast: optional Vast offer id (same as admin upload Vast field).
    vast_offer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Last Vast S3 job prefix for admin retry after worker failure (input kept until success).
    vast_pending_job_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    vast_pending_input_ext: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Active Celery task for admin cancel (download / local encode / Vast transcode).
    pipeline_celery_task_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pipeline_celery_task_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Absolute path on API/worker shared volume for direct uploads (retry after admin cancel).
    pipeline_staging_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
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


class SeriesSeasonMeta(Base):
    """Admin-defined season poster, note, and synopsis per series_key (matches Film.series_key)."""

    __tablename__ = "series_season_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    poster_path: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    synopsis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("series_key", "season_number", name="uq_series_season_meta_key_sn"),
    )


class SeriesShowMeta(Base):
    """Admin-defined global series page: hero poster and text between title and synopsis."""

    __tablename__ = "series_show_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    poster_path: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    hero_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class InvitationCode(Base):
    __tablename__ = "invitation_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    # Set when a member generates a code via POST /api/auth/member-invite; admin codes stay null.
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ViewerAnnouncement(Base):
    """Singleton row id=1: global message for logged-in viewers until ends_at."""

    __tablename__ = "viewer_announcement"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SupportTicket(Base):
    """Viewer-submitted support ticket (content request, bug, suggestion, account, …)."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[SupportTicketCategory] = mapped_column(
        Enum(SupportTicketCategory, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[SupportTicketStatus] = mapped_column(
        Enum(SupportTicketStatus, values_callable=lambda x: [e.value for e in x]),
        default=SupportTicketStatus.open,
        index=True,
    )
    admin_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Last admin who posted a public reply (denormalized for list UI).
    last_admin_reply_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(
        "User",
        back_populates="support_tickets",
        foreign_keys=[user_id],
    )
    last_admin_reply_user: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[last_admin_reply_user_id],
        viewonly=True,
    )
    messages: Mapped[List["SupportTicketMessage"]] = relationship(
        "SupportTicketMessage",
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="SupportTicketMessage.created_at",
    )

    __table_args__ = (Index("ix_support_tickets_created_at", "created_at"),)


class SupportTicketMessage(Base):
    """Public thread message on a support ticket (viewer or admin)."""

    __tablename__ = "support_ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ticket: Mapped["SupportTicket"] = relationship("SupportTicket", back_populates="messages")
    author: Mapped["User"] = relationship(
        "User",
        back_populates="support_ticket_messages",
        foreign_keys=[author_id],
    )

    __table_args__ = (Index("ix_support_ticket_messages_ticket_created", "ticket_id", "created_at"),)


class DonationSettings(Base):
    """Singleton row id=1: crypto donation addresses, EUR goal, cached balance snapshot."""

    __tablename__ = "donation_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal_eur: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    address_btc: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    address_polygon: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    address_solana: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    address_xrp: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    address_tron: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # Campaign visibility window (stored as UTC naive); optional recurrence rolls the window.
    campaign_start_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    campaign_end_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    recurrence: Mapped[Optional[str]] = mapped_column(String(24), nullable=True, default="none")
    snapshot_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
