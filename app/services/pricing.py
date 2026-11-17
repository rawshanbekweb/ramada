"""Rasmiy narx hisoblash (FT-10, FT-12). Reception narxni o'zgartira olmaydi."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.enums import DayType
from app.models import Holiday, Season, Tariff
from app.services import settings_svc as cfg


class PricingError(Exception):
    pass


@dataclass
class Quote:
    lines: list[dict]

    @property
    def total(self) -> int:
        return sum(line["amount"] for line in self.lines)


def day_type(db: Session, d: date) -> str:
    if db.scalar(select(Holiday.id).where(Holiday.day == d)):
        return DayType.HOLIDAY
    # Juma va shanba kechalari — dam olish kuni tarifi
    return DayType.WEEKEND if d.weekday() in (4, 5) else DayType.WEEKDAY


def season_for(db: Session, d: date) -> Season | None:
    return db.scalars(
        select(Season)
        .where(Season.start_date <= d, Season.end_date >= d)
        .order_by(Season.priority.desc(), Season.id.desc())
    ).first()


def night_price(db: Session, category_id: int, d: date) -> tuple[int, str, str | None]:
    dt = day_type(db, d)
    season = season_for(db, d)
    candidates = []
    if season is not None:
        candidates.append((dt, season.id))
    candidates.append((dt, None))
    if dt != DayType.WEEKDAY:
        if season is not None:
            candidates.append((DayType.WEEKDAY, season.id))
        candidates.append((DayType.WEEKDAY, None))
    for cand_dt, season_id in candidates:
        price = db.scalar(
            select(Tariff.price).where(
                Tariff.category_id == category_id,
                Tariff.day_type == cand_dt,
                Tariff.season_id.is_(None) if season_id is None else Tariff.season_id == season_id,
            )
        )
        if price is not None:
            return int(price), dt, season.name if season and season_id else None
    raise PricingError(f"Toifa {category_id} uchun tarif topilmadi")


def stay_lines(db: Session, category_id: int, checkin: date, nights: int) -> list[dict]:
    if nights < 1:
        raise PricingError("Tunlar soni kamida 1")
    lines = []
    for i in range(nights):
        d = checkin + timedelta(days=i)
        price, dt, season = night_price(db, category_id, d)
        lines.append(
            {"kind": "night", "date": d.isoformat(), "day_type": dt, "season": season, "amount": price}
        )
    return lines


def early_checkin_line(db: Session, category_id: int, checkin: date, arrival: datetime) -> dict | None:
    """FT-12: belgilangan kirish soatidan oldin kelish uchun qo'shimcha haq."""
    local = clock.local(arrival)
    if local.date() != checkin:
        return None
    checkin_hour = cfg.get_int(db, "checkin_hour")
    if local.hour >= checkin_hour:
        return None
    half_from = cfg.get_int(db, "early_checkin_half_from_hour")
    prev_night_price, _, _ = night_price(db, category_id, checkin - timedelta(days=1))
    if local.hour >= half_from:
        pct = cfg.get_int(db, "early_late_half_percent")
    else:
        pct = 100
    return {
        "kind": "early_checkin",
        "date": checkin.isoformat(),
        "percent": pct,
        "amount": prev_night_price * pct // 100,
    }


def late_checkout_line(
    db: Session, category_id: int, checkout: date, actual: datetime
) -> dict | None:
    """FT-12: belgilangan chiqish soatidan keyin chiqish uchun qo'shimcha haq."""
    local = clock.local(actual)
    checkout_hour = cfg.get_int(db, "checkout_hour")
    deadline = clock.local(clock.local_dt(checkout, checkout_hour))
    if local <= deadline + timedelta(minutes=15):  # 15 daqiqa bag'rikenglik
        return None
    half_until = clock.local(clock.local_dt(checkout, cfg.get_int(db, "late_checkout_half_until_hour")))
    last_night_price, _, _ = night_price(db, category_id, checkout - timedelta(days=1))
    if local.date() > checkout or local > half_until:
        pct = 100
    else:
        pct = cfg.get_int(db, "early_late_half_percent")
    return {
        "kind": "late_checkout",
        "date": checkout.isoformat(),
        "percent": pct,
        "amount": last_night_price * pct // 100,
    }


def quote(
    db: Session,
    category_id: int,
    checkin: date,
    nights: int,
    arrival: datetime | None = None,
) -> Quote:
    lines = stay_lines(db, category_id, checkin, nights)
    if arrival is not None:
        early = early_checkin_line(db, category_id, checkin, arrival)
        if early:
            lines.insert(0, early)
    return Quote(lines)


def fmt_sum(amount: int | None) -> str:
    if amount is None:
        return "—"
    return f"{amount:,}".replace(",", " ") + " so'm"
