"""FastAPI dependencies."""

from typing import Optional

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from core.security import decode_access_token
from db.models import User, UserRole
from db.session import get_db


def get_current_user(
    db: Session = Depends(get_db),
    redwood_access: Optional[str] = Cookie(default=None),
) -> User:
    if not redwood_access:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_access_token(redwood_access)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")
    return user


def _user_is_admin(user: User) -> bool:
    """Robust admin check (ORM enum, str Enum edge cases, string from raw drivers)."""
    r = user.role
    if r is None:
        return False
    if isinstance(r, UserRole):
        return r == UserRole.admin
    return str(r).lower() in ("admin", UserRole.admin.value)


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not _user_is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user
