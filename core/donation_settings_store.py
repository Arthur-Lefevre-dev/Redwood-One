"""Singleton donation_settings row (shared by API routes and Celery worker)."""

from sqlalchemy.orm import Session

from db.models import DonationSettings

_ROW_ID = 1


def get_or_create_donation_settings(db: Session) -> DonationSettings:
    row = db.get(DonationSettings, _ROW_ID)
    if row is None:
        row = DonationSettings(id=_ROW_ID)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row
