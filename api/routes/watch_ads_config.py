"""Public read-only config for third-party ad tags on watch pages (no secrets)."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import get_settings

router = APIRouter(prefix="/api/public", tags=["public"])


def _safe_https_script_src(raw: str) -> Optional[str]:
    """Reject obvious junk; publisher pastes URL from Coinzilla dashboard."""
    u = (raw or "").strip()
    if not u or len(u) > 2048 or not u.startswith("https://"):
        return None
    if any(c in u for c in "\n\r\t\x00<>'\""):
        return None
    return u


def _safe_zone_id(raw: str) -> Optional[str]:
    z = (raw or "").strip()
    if not z or len(z) > 120:
        return None
    if not re.fullmatch(r"[\w\-]+", z):
        return None
    return z


@router.get("/watch-ads")
def watch_ads_public_config() -> JSONResponse:
    """Film / episode page loads this and injects Coinzilla tag when enabled."""
    s = get_settings()
    src = _safe_https_script_src(getattr(s, "WATCH_ADS_COINZILLA_SCRIPT_SRC", "") or "")
    enabled = bool(getattr(s, "WATCH_ADS_COINZILLA_ENABLED", False)) and bool(src)
    zone = _safe_zone_id(getattr(s, "WATCH_ADS_COINZILLA_ZONE_ID", "") or "")
    body: Dict[str, Any] = {
        "coinzilla": {
            "enabled": enabled,
            "script_src": src if enabled else None,
            "zone_id": zone if enabled else None,
        }
    }
    return JSONResponse(
        content=body,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
