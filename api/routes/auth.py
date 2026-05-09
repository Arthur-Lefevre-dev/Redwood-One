"""Authentication routes (JWT in httpOnly cookies)."""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from api.deps import get_current_user
from api.limits import limiter
from config import get_settings
from core.security import (
    create_access_token,
    create_refresh_token_jwt,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from db.models import InvitationCode, RefreshToken, User, UserRole
from db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    invite_code: Optional[str] = None


class PreferencesBody(BaseModel):
    favorite_genres: List[str] = []


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


@router.post("/register")
@limiter.limit("10 per hour")
def register(request: Request, body: RegisterBody, db: Session = Depends(get_db)):
    settings = get_settings()
    code = (body.invite_code or "").strip() or None
    if not settings.REGISTRATION_OPEN:
        if not code:
            raise HTTPException(status_code=400, detail="Invitation code required")
        inv = db.query(InvitationCode).filter(InvitationCode.code == code).first()
        if not inv or inv.uses >= inv.max_uses:
            raise HTTPException(status_code=400, detail="Invalid invitation code")
        if inv.expires_at and inv.expires_at < datetime.utcnow():
            raise HTTPException(status_code=400, detail="Invitation expired")
    elif code:
        inv = db.query(InvitationCode).filter(InvitationCode.code == code).first()
        if not inv or inv.uses >= inv.max_uses:
            inv = None
        elif inv.expires_at and inv.expires_at < datetime.utcnow():
            inv = None
    else:
        inv = None

    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        username=body.username.strip(),
        email=body.email.lower().strip(),
        hashed_password=hash_password(body.password),
        role=UserRole.viewer,
        preferences={"favorite_genres": []},
    )
    db.add(user)
    if inv:
        inv.uses += 1
    db.commit()
    return {"ok": True, "username": user.username}


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
        "preferences": user.preferences if isinstance(user.preferences, dict) else {},
    }


@router.patch("/me/preferences")
def patch_preferences(
    body: PreferencesBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    prev = user.preferences if isinstance(user.preferences, dict) else {}
    prev = dict(prev)
    prev["favorite_genres"] = [str(g).strip() for g in body.favorite_genres if str(g).strip()]
    user.preferences = prev
    db.commit()
    return {"ok": True, "preferences": user.preferences}
