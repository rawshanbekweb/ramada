"""Vaqt manbai. Testlarda `set_now()` orqali vaqtni boshqarish mumkin."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings

UTC = timezone.utc
_frozen: datetime | None = None


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now() -> datetime:
    """Joriy vaqt (UTC, timezone-aware)."""
    if _frozen is not None:
        return _frozen
    return datetime.now(UTC)


def set_now(value: datetime | None) -> None:
    global _frozen
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=tz())
    _frozen = value.astimezone(UTC) if value else None


def advance(**kwargs) -> datetime:
    global _frozen
    if _frozen is None:
        _frozen = datetime.now(UTC)
    _frozen = _frozen + timedelta(**kwargs)
    return _frozen


def local(dt: datetime | None = None) -> datetime:
    return (dt or now()).astimezone(tz())


def today() -> date:
    return local().date()


def local_dt(d: date, hour: int = 0, minute: int = 0) -> datetime:
    """Mahalliy sana+soatni UTC datetime ga aylantiradi."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz()).astimezone(UTC)


def fmt(dt: datetime | None, with_date: bool = True) -> str:
    if dt is None:
        return "—"
    d = local(dt)
    return d.strftime("%d.%m.%Y %H:%M") if with_date else d.strftime("%H:%M")


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
