"""Member-generated invitation codes: monthly quota, listing, admin reset."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from db.models import InvitationCode, User, UserRole, ViewerRank

# Monthly member-invite quota (UTC calendar month) per viewer rank.
VIEWER_RANK_QUOTAS: Dict[ViewerRank, int] = {
    ViewerRank.bronze: 1,
    ViewerRank.silver: 3,
    ViewerRank.gold: 6,
    ViewerRank.platinum: 10,
}


def effective_viewer_rank(user: User) -> ViewerRank:
    """Rank used for invitation quota (admins get platinum-equivalent)."""
    if user.role == UserRole.admin:
        return ViewerRank.platinum
    raw = (user.viewer_rank or "").strip().lower()
    try:
        return ViewerRank(raw)
    except ValueError:
        return ViewerRank.bronze


def monthly_invite_quota(user: User) -> int:
    return VIEWER_RANK_QUOTAS[effective_viewer_rank(user)]


def legacy_invite_note_clause(user: User):
    """
    Legacy rows without created_by_user_id: note starts with 'Invité par <username>'.
    LIKE-escape % and _ in username.
    """
    u = (user.username or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"Invité par {u}%"
    return and_(
        InvitationCode.created_by_user_id.is_(None),
        InvitationCode.note.isnot(None),
        InvitationCode.note.like(pattern, escape="\\"),
    )


def member_invite_clause(user: User):
    return or_(InvitationCode.created_by_user_id == user.id, legacy_invite_note_clause(user))


def month_bounds_utc_naive() -> tuple[datetime, datetime]:
    """Calendar month [start, end) in naive UTC (matches invitation_codes.created_at)."""
    now = datetime.utcnow()
    start = datetime(now.year, now.month, 1)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1)
    else:
        end = datetime(now.year, now.month + 1, 1)
    return start, end


def serialize_member_invite_row(row: InvitationCode) -> Dict[str, Any]:
    now = datetime.utcnow()
    expired = bool(row.expires_at and row.expires_at < now)
    exhausted = row.uses >= row.max_uses
    return {
        "code": row.code,
        "max_uses": row.max_uses,
        "uses": row.uses,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "usable": not exhausted and not expired,
    }


def list_member_invites_payload(db: Session, user: User) -> List[Dict[str, Any]]:
    rows = (
        db.query(InvitationCode)
        .filter(member_invite_clause(user))
        .order_by(InvitationCode.created_at.desc())
        .limit(100)
        .all()
    )
    return [serialize_member_invite_row(r) for r in rows]


def member_invites_this_month_count(db: Session, user: User) -> int:
    start, end = month_bounds_utc_naive()
    return (
        db.query(InvitationCode)
        .filter(
            member_invite_clause(user),
            InvitationCode.created_at >= start,
            InvitationCode.created_at < end,
        )
        .count()
    )


def invite_month_status(db: Session, user: User) -> Dict[str, Any]:
    """Member invites per UTC month; quota depends on viewer rank."""
    n = member_invites_this_month_count(db, user)
    quota = monthly_invite_quota(user)
    rank = effective_viewer_rank(user)
    base = {
        "invites_created_this_month": n,
        "invite_quota_per_month": quota,
        "viewer_rank": rank.value,
    }
    if n < quota:
        return {
            **base,
            "can_invite_this_month": True,
            "next_invite_at": None,
        }
    now = datetime.now(timezone.utc)
    if now.month == 12:
        y, m = now.year + 1, 1
    else:
        y, m = now.year, now.month + 1
    next_start = datetime(y, m, 1, tzinfo=timezone.utc)
    return {
        **base,
        "can_invite_this_month": False,
        "next_invite_at": next_start.isoformat(),
    }


def reset_member_invite_quota_current_month(db: Session, user: User) -> int:
    """
    Remove member-generated invite rows for the current UTC month so the user can create one again.
    Also clears last_invite_at. Returns number of deleted codes.
    """
    start, end = month_bounds_utc_naive()
    rows = (
        db.query(InvitationCode)
        .filter(
            member_invite_clause(user),
            InvitationCode.created_at >= start,
            InvitationCode.created_at < end,
        )
        .all()
    )
    deleted = 0
    for row in rows:
        db.delete(row)
        deleted += 1
    user.last_invite_at = None
    db.add(user)
    db.commit()
    db.refresh(user)
    return deleted
