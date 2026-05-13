"""Support tickets: viewers submit and reply; admins list, update status, and post replies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import asc, desc, func
from sqlalchemy.orm import Session, joinedload

from api.deps import get_current_user, require_admin
from db.models import (
    SupportTicket,
    SupportTicketCategory,
    SupportTicketMessage,
    SupportTicketStatus,
    User,
    UserRole,
)
from db.session import get_db

viewer_router = APIRouter(prefix="/api/support-tickets", tags=["support-tickets"])
admin_router = APIRouter(prefix="/api/admin/support-tickets", tags=["admin-support-tickets"])

_MAX_BODY = 16_000


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _user_is_admin(user: User) -> bool:
    r = user.role
    if r is None:
        return False
    if isinstance(r, UserRole):
        return r == UserRole.admin
    return str(r).lower() in ("admin", UserRole.admin.value)


def _serialize_message(m: SupportTicketMessage) -> Dict[str, Any]:
    au = m.author
    role = "admin" if au and _user_is_admin(au) else "viewer"
    return {
        "id": m.id,
        "author_username": au.username if au else "—",
        "author_role": role,
        "body": m.body,
        "created_at": _iso_utc(m.created_at),
    }


def _db_messages_for_ticket(db: Session, ticket_id: int) -> List[SupportTicketMessage]:
    return (
        db.query(SupportTicketMessage)
        .options(joinedload(SupportTicketMessage.author))
        .filter(SupportTicketMessage.ticket_id == ticket_id)
        .order_by(asc(SupportTicketMessage.created_at))
        .all()
    )


def _thread_messages(db: Session, ticket: SupportTicket) -> List[Dict[str, Any]]:
    rows = _db_messages_for_ticket(db, ticket.id)
    out: List[Dict[str, Any]] = [_serialize_message(m) for m in rows]
    if not out and (ticket.admin_response or "").strip():
        out.append(
            {
                "id": None,
                "author_username": "Administrateur",
                "author_role": "admin",
                "body": (ticket.admin_response or "").strip(),
                "created_at": _iso_utc(ticket.updated_at) or _iso_utc(ticket.created_at),
                "legacy": True,
            }
        )
    return out


def _serialize_viewer_core(t: SupportTicket) -> Dict[str, Any]:
    return {
        "id": t.id,
        "category": t.category.value,
        "subject": t.subject,
        "body": t.body,
        "status": t.status.value,
        "admin_response": t.admin_response,
        "last_reply_admin_username": None,
        "created_at": _iso_utc(t.created_at),
        "updated_at": _iso_utc(t.updated_at),
        "resolved_at": _iso_utc(t.resolved_at),
    }


def _last_admin_display_name(db: Session, t: SupportTicket) -> Optional[str]:
    if t.last_admin_reply_user_id:
        u = db.get(User, t.last_admin_reply_user_id)
        return u.username if u else None
    msgs = _db_messages_for_ticket(db, t.id)
    for m in reversed(msgs):
        if m.author and _user_is_admin(m.author):
            return m.author.username
    if (t.admin_response or "").strip() and not msgs:
        return "Administrateur"
    return None


def _serialize_viewer(db: Session, t: SupportTicket, *, include_thread: bool) -> Dict[str, Any]:
    out = _serialize_viewer_core(t)
    out["last_reply_admin_username"] = _last_admin_display_name(db, t)
    if include_thread:
        out["messages"] = _thread_messages(db, t)
    return out


def _serialize_admin(db: Session, t: SupportTicket, *, include_thread: bool) -> Dict[str, Any]:
    u = t.user
    out = _serialize_viewer(db, t, include_thread=include_thread)
    out["user_id"] = t.user_id
    out["user_username"] = u.username if u else None
    out["user_email"] = u.email if u else None
    return out


class CreateSupportTicketBody(BaseModel):
    category: SupportTicketCategory
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=10, max_length=_MAX_BODY)

    @field_validator("subject", "body")
    @classmethod
    def strip_text(cls, v: str) -> str:
        return (v or "").strip()


class ViewerReplyBody(BaseModel):
    body: str = Field(min_length=1, max_length=_MAX_BODY)

    @field_validator("body")
    @classmethod
    def strip_body(cls, v: str) -> str:
        return (v or "").strip()


class AdminPatchSupportTicketBody(BaseModel):
    status: Optional[SupportTicketStatus] = None
    # New public reply from the current admin (appended as a thread message).
    admin_response: Optional[str] = Field(default=None, max_length=_MAX_BODY)

    @field_validator("admin_response")
    @classmethod
    def strip_response(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        t = v.strip()
        return t if t else None


@viewer_router.get("")
def list_my_tickets(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(100, ge=1, le=200),
) -> Dict[str, Any]:
    """List tickets created by the current user (newest first)."""
    q = (
        db.query(SupportTicket)
        .filter(SupportTicket.user_id == user.id)
        .order_by(desc(SupportTicket.created_at))
        .limit(limit)
    )
    rows = q.all()
    return {
        "tickets": [_serialize_viewer(db, t, include_thread=False) for t in rows],
        "count": len(rows),
    }


@viewer_router.post("", status_code=201)
def create_ticket(
    body: CreateSupportTicketBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Open a new support ticket."""
    now = datetime.utcnow()
    t = SupportTicket(
        user_id=user.id,
        category=body.category,
        subject=body.subject,
        body=body.body,
        status=SupportTicketStatus.open,
        created_at=now,
        updated_at=now,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return _serialize_viewer(db, t, include_thread=True)


@viewer_router.post("/{ticket_id}/replies", status_code=201)
def post_viewer_reply(
    ticket_id: int,
    body: ViewerReplyBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Append a viewer reply on an owned ticket. Replies are not allowed when status is resolved or closed."""
    t = db.get(SupportTicket, ticket_id)
    if not t or t.user_id != user.id:
        raise HTTPException(status_code=404, detail="Ticket introuvable")
    if t.status in (SupportTicketStatus.resolved, SupportTicketStatus.closed):
        raise HTTPException(
            status_code=403,
            detail="Ce ticket est résolu ou fermé ; vous ne pouvez plus y répondre.",
        )
    now = datetime.utcnow()
    msg = SupportTicketMessage(ticket_id=t.id, author_id=user.id, body=body.body, created_at=now)
    db.add(msg)
    t.updated_at = now
    db.add(t)
    db.commit()
    db.refresh(t)
    return _serialize_viewer(db, t, include_thread=True)


@viewer_router.get("/{ticket_id}")
def get_my_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    t = db.get(SupportTicket, ticket_id)
    if not t or t.user_id != user.id:
        raise HTTPException(status_code=404, detail="Ticket introuvable")
    return _serialize_viewer(db, t, include_thread=True)


@admin_router.get("")
def admin_list_tickets(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    status_filter: Optional[SupportTicketStatus] = Query(None, alias="status"),
    category_filter: Optional[SupportTicketCategory] = Query(None, alias="category"),
    limit: int = Query(80, ge=1, le=200),
    offset: int = Query(0, ge=0, le=50_000),
) -> Dict[str, Any]:
    """List all support tickets (admin)."""
    q = db.query(SupportTicket)
    if status_filter is not None:
        q = q.filter(SupportTicket.status == status_filter)
    if category_filter is not None:
        q = q.filter(SupportTicket.category == category_filter)
    total = q.count()
    rows = (
        q.options(joinedload(SupportTicket.user))
        .order_by(desc(SupportTicket.created_at))
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "tickets": [_serialize_admin(db, t, include_thread=False) for t in rows],
        "count": len(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@admin_router.get("/pending-count")
def admin_support_tickets_pending_count(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Count tickets awaiting action (open or in progress)."""
    n = (
        db.query(func.count(SupportTicket.id))
        .filter(
            SupportTicket.status.in_(
                (SupportTicketStatus.open, SupportTicketStatus.in_progress)
            )
        )
        .scalar()
    )
    return {"pending": int(n or 0)}


@admin_router.get("/{ticket_id}")
def admin_get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> Dict[str, Any]:
    t = db.get(SupportTicket, ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Ticket introuvable")
    return _serialize_admin(db, t, include_thread=True)


@admin_router.patch("/{ticket_id}")
def admin_patch_ticket(
    ticket_id: int,
    body: AdminPatchSupportTicketBody,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
) -> Dict[str, Any]:
    t = db.get(SupportTicket, ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Ticket introuvable")
    if body.status is None and body.admin_response is None:
        raise HTTPException(
            status_code=400,
            detail="Fournissez au moins status ou admin_response",
        )
    now = datetime.utcnow()
    if body.status is not None:
        t.status = body.status
        if body.status in (SupportTicketStatus.resolved, SupportTicketStatus.closed):
            t.resolved_at = now
        else:
            t.resolved_at = None
    if body.admin_response is not None:
        reply = body.admin_response
        m = SupportTicketMessage(
            ticket_id=t.id,
            author_id=admin_user.id,
            body=reply,
            created_at=now,
        )
        db.add(m)
        t.admin_response = reply
        t.last_admin_reply_user_id = admin_user.id
    t.updated_at = now
    db.add(t)
    db.commit()
    db.refresh(t)
    return _serialize_admin(db, t, include_thread=True)
