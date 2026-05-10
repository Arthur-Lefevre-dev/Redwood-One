"""Public donation progress for authenticated viewers."""

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.deps import get_current_user
from core.donation_campaign import effective_campaign_window, normalize_recurrence
from db.models import DonationSettings, User
from db.session import get_db

_ROW_ID = 1

router = APIRouter(prefix="/api/donations", tags=["donations"])


def _period_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_or_create_donation_settings(db: Session) -> DonationSettings:
    row = db.get(DonationSettings, _ROW_ID)
    if row is None:
        row = DonationSettings(id=_ROW_ID)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("/progress")
def donation_progress(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> Dict[str, Any]:
    row = get_or_create_donation_settings(db)
    goal = row.goal_eur
    snap = row.snapshot_json if isinstance(row.snapshot_json, dict) else None
    raised = None
    updated = None
    if snap:
        raised = snap.get("raised_eur")
        updated = snap.get("fetched_at")

    eff_start, eff_end, in_campaign = effective_campaign_window(
        row.campaign_start_utc,
        row.campaign_end_utc,
        normalize_recurrence(row.recurrence),
    )

    if goal is None or float(goal) <= 0 or not in_campaign:
        return {
            "visible": False,
            "goal_eur": None,
            "raised_eur": None,
            "progress_percent": None,
            "updated_at": updated,
            "period_start_utc": _period_iso(eff_start),
            "period_end_utc": _period_iso(eff_end),
            "in_campaign": in_campaign,
            "recurrence": normalize_recurrence(row.recurrence),
        }
    goal_f = float(goal)
    raised_f = float(raised) if raised is not None else 0.0
    pct = min(100.0, (raised_f / goal_f) * 100.0) if goal_f > 0 else 0.0
    return {
        "visible": True,
        "goal_eur": round(goal_f, 2),
        "raised_eur": round(raised_f, 2),
        "progress_percent": round(pct, 1),
        "updated_at": updated,
        "period_start_utc": _period_iso(eff_start),
        "period_end_utc": _period_iso(eff_end),
        "in_campaign": in_campaign,
        "recurrence": normalize_recurrence(row.recurrence),
    }
