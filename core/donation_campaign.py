"""Effective donation campaign time window with optional recurrence (naive UTC datetimes)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

RECURRENCE_NONE = "none"
RECURRENCE_REPEAT_SPAN = "repeat_span"
RECURRENCE_WEEKLY = "weekly"
RECURRENCE_MONTHLY = "monthly"
RECURRENCE_YEARLY = "yearly"

VALID_RECURRENCE = frozenset(
    {
        RECURRENCE_NONE,
        RECURRENCE_REPEAT_SPAN,
        RECURRENCE_WEEKLY,
        RECURRENCE_MONTHLY,
        RECURRENCE_YEARLY,
    }
)


def normalize_recurrence(raw: Optional[str]) -> str:
    v = (raw or RECURRENCE_NONE).strip().lower()
    return v if v in VALID_RECURRENCE else RECURRENCE_NONE


def now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def effective_campaign_window(
    start: Optional[datetime],
    end: Optional[datetime],
    recurrence: str,
    *,
    now: Optional[datetime] = None,
) -> Tuple[Optional[datetime], Optional[datetime], bool]:
    """
    Compute the current period [period_start, period_end] and whether now is inside it.

    - No start and no end: no time limit (always in window).
    - Only end: visible if now <= end.
    - Only start: visible if now >= start.
    - Both: apply recurrence; inclusive bounds on both ends.
    """
    now = now or now_utc_naive()
    rec = normalize_recurrence(recurrence)

    if start is None and end is None:
        return None, None, True

    if start is None and end is not None:
        return None, end, now <= end

    if start is not None and end is None:
        return start, None, now >= start

    assert start is not None and end is not None
    if end < start:
        return start, end, False

    if rec == RECURRENCE_NONE:
        return start, end, start <= now <= end

    s, e = start, end
    if rec == RECURRENCE_REPEAT_SPAN:
        span = e - s
        if span <= timedelta(0):
            return s, e, False
        while now > e:
            s, e = e, e + span
        return s, e, s <= now <= e

    step = {
        RECURRENCE_WEEKLY: timedelta(days=7),
        RECURRENCE_MONTHLY: timedelta(days=30),
        RECURRENCE_YEARLY: timedelta(days=365),
    }.get(rec, timedelta(0))
    if step <= timedelta(0):
        return s, e, s <= now <= e
    while now > e:
        s = e
        e = s + step
    return s, e, s <= now <= e
