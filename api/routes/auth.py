"""Authentication routes (JWT in httpOnly cookies)."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.deps import get_current_user
from api.limits import limiter
from config import get_settings
from core.security import (
    create_access_token,
    create_refresh_token_jwt,
    hash_refresh_token,
    verify_password,
)
from db.models import RefreshToken, User
from db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


def _cookie_kwargs():
    return {
        "httponly": True,
        "secure": False,
        "samesite": "lax",
        "path": "/",
    }


@router.post("/login")
@limiter.limit("5 per 15 minutes")
def login(
    request: Request,
    body: LoginBody,
    response: Response,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")

    settings = get_settings()
    access = create_access_token({"sub": str(user.id), "role": user.role.value})
    raw_refresh = create_refresh_token_jwt(user.id)
    rt = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(rt)
    user.derniere_connexion = datetime.now(timezone.utc)
    db.commit()

    ck = _cookie_kwargs()
    response.set_cookie(
        "redwood_access",
        access,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        **ck,
    )
    response.set_cookie(
        "redwood_refresh",
        raw_refresh,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        **ck,
    )
    return {"ok": True, "username": user.username, "role": user.role.value}


@router.post("/refresh")
def refresh_token(
    response: Response,
    db: Session = Depends(get_db),
    redwood_refresh: str | None = Cookie(default=None),
):
    if not redwood_refresh:
        raise HTTPException(status_code=401, detail="Missing refresh")
    settings = get_settings()
    h = hash_refresh_token(redwood_refresh)
    row = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.token_hash == h,
            RefreshToken.revoked.is_(False),
        )
        .first()
    )
    if not row or row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Invalid refresh")
    user = db.get(User, row.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid user")

    row.revoked = True
    access = create_access_token({"sub": str(user.id), "role": user.role.value})
    raw_refresh = create_refresh_token_jwt(user.id)
    rt = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(rt)
    db.commit()

    ck = _cookie_kwargs()
    response.set_cookie(
        "redwood_access",
        access,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        **ck,
    )
    response.set_cookie(
        "redwood_refresh",
        raw_refresh,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        **ck,
    )
    return {"ok": True}


@router.post("/logout")
def logout(
    response: Response,
    db: Session = Depends(get_db),
    redwood_refresh: str | None = Cookie(default=None),
):
    response.delete_cookie("redwood_access", path="/")
    response.delete_cookie("redwood_refresh", path="/")
    if redwood_refresh:
        h = hash_refresh_token(redwood_refresh)
        db.query(RefreshToken).filter(RefreshToken.token_hash == h).update({"revoked": True})
        db.commit()
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
    }
