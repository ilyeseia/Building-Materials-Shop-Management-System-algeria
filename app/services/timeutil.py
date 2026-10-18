"""Time helpers bound to the business time zone (``Africa/Algiers`` by default).

* Audit timestamps are stored as **naive UTC** so that ordering and comparisons
  never depend on the server's local settings.
* *Business* dates (order date, delivery date, payment date) are plain
  :class:`datetime.date` values interpreted in the business time zone.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from config import Config


@lru_cache(maxsize=8)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def business_timezone() -> ZoneInfo:
    """The active business time zone.

    Read from BusinessSettings when the database is available, otherwise from
    configuration, so the helper also works during startup and in tests.
    """
    name = None
    try:
        from app.services.settings import get_settings

        settings = get_settings()
        if settings is not None and settings.timezone:
            name = settings.timezone
    except Exception:  # pragma: no cover - settings may not exist yet
        name = None
    name = name or Config.TIMEZONE
    try:
        return _zone(name)
    except Exception:  # pragma: no cover - invalid configuration
        return _zone("UTC")


def utcnow() -> datetime:
    """Naive UTC timestamp for storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_local() -> datetime:
    """Time-zone aware current time in the business zone."""
    return datetime.now(timezone.utc).astimezone(business_timezone())


def today_local() -> date:
    """Today's date in the business time zone."""
    return now_local().date()


def to_local(naive_utc: datetime | None) -> datetime | None:
    """Attach the business zone to a stored naive-UTC timestamp."""
    if naive_utc is None:
        return None
    if naive_utc.tzinfo is None:
        naive_utc = naive_utc.replace(tzinfo=timezone.utc)
    return naive_utc.astimezone(business_timezone())


def month_start(day: date | None = None) -> date:
    day = day or today_local()
    return day.replace(day=1)


def add_days(day: date, days: int) -> date:
    from datetime import timedelta

    return day + timedelta(days=days)
