"""Viewer-facing global announcement (configured by admin)."""

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.deps import get_current_user
from db.models import AuthPageAnnouncement, User, ViewerAnnouncement
from db.session import get_db

router = APIRouter(prefix="/api", tags=["announcement"])

_ROW_ID = 1


def _get_or_create_row(db: Session) -> ViewerAnnouncement:
    row = db.get(ViewerAnnouncement, _ROW_ID)
    if row is None:
        row = ViewerAnnouncement(id=_ROW_ID, message=None, ends_at=None)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _is_active(row: ViewerAnnouncement) -> bool:
    if not row.message or not str(row.message).strip():
        return False
    if row.ends_at is None:
        return False
    return datetime.utcnow() < row.ends_at


@router.get("/announcement")
def get_active_announcement(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return the current announcement if active (authenticated viewers only)."""
    row = _get_or_create_row(db)
    if not _is_active(row):
        return {"active": False}
    ends = row.ends_at
    return {
        "active": True,
        "message": row.message.strip(),
        "ends_at": ends.strftime("%Y-%m-%dT%H:%M:%SZ") if ends else None,
    }


def _serialize_public_auth_row(row: AuthPageAnnouncement) -> Dict[str, Any]:
    return {
        "id": row.id,
        "title": (row.title or "").strip() or None,
        "body": row.body or "",
    }


@router.get("/public/auth-page-announcements")
def get_auth_page_announcements_public(
    placement: str = Query(..., description="login or register"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Active notices for login.html / register.html (no authentication)."""
    p = (placement or "").strip().lower()
    if p not in ("login", "register"):
        raise HTTPException(status_code=400, detail="placement must be login or register")
    rows: List[AuthPageAnnouncement] = (
        db.query(AuthPageAnnouncement)
        .filter(
            AuthPageAnnouncement.is_active.is_(True),
            or_(AuthPageAnnouncement.placement == p, AuthPageAnnouncement.placement == "both"),
        )
        .order_by(AuthPageAnnouncement.sort_order.asc(), AuthPageAnnouncement.id.asc())
        .all()
    )
    items = [_serialize_public_auth_row(r) for r in rows if (r.body or "").strip()]
    return JSONResponse(
        content={"items": items},
        headers={"Cache-Control": "public, max-age=60"},
    )
