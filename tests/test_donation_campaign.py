"""Donation campaign time window + recurrence."""

from datetime import datetime, timedelta

from core.donation_campaign import (
    RECURRENCE_MONTHLY,
    RECURRENCE_NONE,
    RECURRENCE_REPEAT_SPAN,
    RECURRENCE_WEEKLY,
    RECURRENCE_YEARLY,
    effective_campaign_window,
)


def test_no_dates_always_visible():
    s, e, ok = effective_campaign_window(None, None, RECURRENCE_NONE, now=datetime(2026, 6, 1, 12, 0, 0))
    assert s is None and e is None and ok is True


def test_none_recurrence_inside_window():
    start = datetime(2026, 5, 1, 0, 0, 0)
    end = datetime(2026, 5, 31, 23, 59, 59)
    s, e, ok = effective_campaign_window(start, end, RECURRENCE_NONE, now=datetime(2026, 5, 10, 12, 0, 0))
    assert s == start and e == end and ok is True


def test_none_recurrence_outside():
    start = datetime(2026, 5, 1, 0, 0, 0)
    end = datetime(2026, 5, 10, 0, 0, 0)
    _, _, ok = effective_campaign_window(start, end, RECURRENCE_NONE, now=datetime(2026, 5, 20, 12, 0, 0))
    assert ok is False


def test_repeat_span_rolls_forward():
    start = datetime(2026, 5, 1, 0, 0, 0)
    end = datetime(2026, 5, 8, 0, 0, 0)
    now = datetime(2026, 5, 20, 12, 0, 0)
    s, e, ok = effective_campaign_window(start, end, RECURRENCE_REPEAT_SPAN, now=now)
    assert ok is True
    assert s == datetime(2026, 5, 15, 0, 0, 0)
    assert e == datetime(2026, 5, 22, 0, 0, 0)


def test_weekly_after_first_period():
    start = datetime(2026, 5, 1, 0, 0, 0)
    end = datetime(2026, 5, 3, 0, 0, 0)
    now = datetime(2026, 5, 10, 12, 0, 0)
    s, e, ok = effective_campaign_window(start, end, RECURRENCE_WEEKLY, now=now)
    assert ok is True
    assert s == datetime(2026, 5, 10, 0, 0, 0)
    assert e == datetime(2026, 5, 17, 0, 0, 0)


def test_monthly_step():
    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 5, 0, 0, 0)
    now = datetime(2026, 2, 1, 12, 0, 0)
    s, e, ok = effective_campaign_window(start, end, RECURRENCE_MONTHLY, now=now)
    assert ok is True
    assert (e - s) == timedelta(days=30)


def test_yearly_step():
    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 10, 0, 0, 0)
    now = datetime(2027, 6, 1, 12, 0, 0)
    s, e, ok = effective_campaign_window(start, end, RECURRENCE_YEARLY, now=now)
    assert ok is True
    assert (e - s) == timedelta(days=365)
