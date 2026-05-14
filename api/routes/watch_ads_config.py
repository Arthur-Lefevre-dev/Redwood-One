"""Public read-only config for third-party ad tags (watch film + auth pages; no secrets)."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import get_settings

router = APIRouter(prefix="/api/public", tags=["public"])


def _safe_aads_unit_id(raw: str) -> Optional[str]:
    """A-ADS unit id is numeric (publisher dashboard)."""
    z = (raw or "").strip()
    if not z or len(z) > 20:
        return None
    if not re.fullmatch(r"\d+", z):
        return None
    return z


@router.get("/watch-ads")
def watch_ads_public_config() -> JSONResponse:
    """Watch film + auth pages load this; inject A-ADS iframes when enabled."""
    s = get_settings()
    unit = _safe_aads_unit_id(getattr(s, "WATCH_ADS_AADS_UNIT_ID", "") or "")
    enabled = bool(getattr(s, "WATCH_ADS_AADS_ENABLED", False)) and bool(unit)
    unit_auth = _safe_aads_unit_id(getattr(s, "WATCH_ADS_AADS_AUTH_UNIT_ID", "") or "")
    auth_enabled = bool(getattr(s, "WATCH_ADS_AADS_AUTH_ENABLED", False)) and bool(unit_auth)
    body: Dict[str, Any] = {
        "aads": {
            "enabled": enabled,
            "unit_id": unit if enabled else None,
        },
        "aads_auth": {
            "enabled": auth_enabled,
            "unit_id": unit_auth if auth_enabled else None,
        },
    }
    return JSONResponse(
        content=body,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
