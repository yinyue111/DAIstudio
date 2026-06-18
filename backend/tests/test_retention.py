"""Retention math (expiry / days_left / expired), including tz-naive coercion."""
from datetime import datetime, timedelta, timezone

from app.services import retention


def test_expiry_and_days_left():
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=10)
    assert retention.is_expired(created, 30) is False
    assert retention.days_left(created, 30) in (19, 20)  # ~20 days remaining

    old = now - timedelta(days=40)
    assert retention.is_expired(old, 30) is True
    assert retention.days_left(old, 30) == 0


def test_naive_datetime_coerced():
    # SQLite may hand back tz-naive datetimes; must not crash and treat as UTC
    naive_old = datetime.utcnow() - timedelta(days=40)
    assert retention.is_expired(naive_old, 30) is True
    naive_new = datetime.utcnow() - timedelta(days=1)
    assert retention.is_expired(naive_new, 30) is False


def test_none_created_at():
    assert retention.is_expired(None, 30) is False
    assert retention.days_left(None, 30) is None
    assert retention.expiry_of(None, 30) is None
