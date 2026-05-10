"""Authentication routes (JWT in httpOnly cookies)."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.deps import get_current_user
from api.limits import limiter
from config import get_settings
from core.email_policy import validate_viewer_email
from core.member_invites import invite_month_status, list_member_invites_payload
from core.password_policy import validate_password_strength
from core.security import (
    create_access_token,
    create_refresh_token_jwt,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from core.member_invites import effective_viewer_rank
from db.models import InvitationCode, RefreshToken, User, UserRole, ViewerRank
from db.session import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Applied at import time; override via AUTH_LOGIN_RATE_LIMIT in env.
_LOGIN_LIMIT = get_settings().AUTH_LOGIN_RATE_LIMIT


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(max_length=128)
    invite_code: Optional[str] = None


class PreferencesBody(BaseModel):
    favorite_genres: List[str] = []


class PatchMeBody(BaseModel):
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = Field(None, max_length=128)


def _cookie_kwargs():
    return {
        "httponly": True,
        "secure": False,
        "samesite": "lax",
        "path": "/",
    }


@router.post("/login")
@limiter.limit(_LOGIN_LIMIT)
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

    email_norm, email_err = validate_viewer_email(str(body.email))
    if email_err or not email_norm:
        raise HTTPException(status_code=400, detail=email_err or "Adresse e-mail invalide.")
    if (
        db.query(User)
        .filter(func.lower(User.email) == email_norm.lower())
        .first()
    ):
        raise HTTPException(status_code=400, detail="Email already registered")

    pw_err = validate_password_strength(
        body.password,
        username=body.username.strip(),
        email=email_norm.lower(),
    )
    if pw_err:
        raise HTTPException(status_code=400, detail=pw_err)

    user = User(
        username=body.username.strip(),
        email=email_norm,
        hashed_password=hash_password(body.password),
        role=UserRole.viewer,
        viewer_rank=ViewerRank.bronze.value,
        preferences={"favorite_genres": []},
        signup_channel="invite" if inv else "open",
        registered_via_invite_code_id=inv.id if inv else None,
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


# Register longer /me/* paths before /me so routers never treat them as missing (defensive ordering).
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


@router.post("/member-invite")
@router.post("/me/invite")
@limiter.limit("30 per hour")
def create_user_invite(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not invite_month_status(db, user)["can_invite_this_month"]:
        raise HTTPException(
            status_code=403,
            detail="Vous avez déjà généré une invitation ce mois-ci (limite : une par mois, UTC).",
        )
    raw = None
    for _ in range(12):
        cand = secrets.token_hex(5).upper()
        if not db.query(InvitationCode).filter(InvitationCode.code == cand).first():
            raw = cand
            break
    if not raw:
        raise HTTPException(status_code=500, detail="Impossible de générer un code unique")
    note = f"Invité par {user.username}"[:255]
    inv = InvitationCode(
        code=raw,
        max_uses=1,
        uses=0,
        note=note,
        expires_at=None,
        created_by_user_id=user.id,
    )
    db.add(inv)
    user.last_invite_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "code": inv.code,
        "max_uses": inv.max_uses,
        "uses": inv.uses,
        "invite": invite_month_status(db, user),
        "my_invites": list_member_invites_payload(db, user),
    }


@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rank = effective_viewer_rank(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
        "viewer_rank": rank.value,
        "preferences": user.preferences if isinstance(user.preferences, dict) else {},
        "invite": invite_month_status(db, user),
        "my_invites": list_member_invites_payload(db, user),
    }


@router.patch("/me")
def patch_me(
    body: PatchMeBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    new_email: str | None = None
    if body.email is not None:
        norm, em_err = validate_viewer_email(str(body.email))
        if em_err or not norm:
            raise HTTPException(status_code=400, detail=em_err or "Adresse e-mail invalide.")
        new_email = norm
    email_change = new_email is not None and new_email.lower() != (user.email or "").lower()
    pw_change = body.new_password is not None
    if not email_change and not pw_change:
        raise HTTPException(status_code=400, detail="Aucune modification demandée")
    if not body.current_password:
        raise HTTPException(status_code=400, detail="Mot de passe actuel requis")
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Mot de passe actuel incorrect")
    if pw_change:
        pw_err = validate_password_strength(
            body.new_password or "",
            username=user.username,
            email=(new_email if email_change else user.email) or user.email,
        )
        if pw_err:
            raise HTTPException(status_code=400, detail=pw_err)
    if email_change:
        taken = (
            db.query(User)
            .filter(func.lower(User.email) == new_email.lower(), User.id != user.id)
            .first()
        )
        if taken:
            raise HTTPException(status_code=400, detail="Cette adresse e-mail est déjà utilisée")
        user.email = new_email
    if pw_change:
        user.hashed_password = hash_password(body.new_password)
    db.add(user)
    db.commit()
    db.refresh(user)
    rank = effective_viewer_rank(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
        "viewer_rank": rank.value,
        "preferences": user.preferences if isinstance(user.preferences, dict) else {},
        "invite": invite_month_status(db, user),
        "my_invites": list_member_invites_payload(db, user),
    }
