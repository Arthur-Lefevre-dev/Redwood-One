"""Viewer-facing global announcement (configured by admin)."""

from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps import get_current_user
from db.models import User, ViewerAnnouncement
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
