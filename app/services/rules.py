"""Tafovut moduli: davriy tekshiruvlar (FT-41). Rejalashtiruvchi har daqiqada chaqiradi."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.enums import BookingStatus
from app.i18n import t
from app.models import Booking
from app.services import alerts, bookings, discounts, entry, housekeeping, notify
from app.services import settings_svc as cfg
from app.services.pricing import fmt_sum

log = logging.getLogger(__name__)


def qr_reminders(db: Session) -> int:
    """FT-25: 3-bosqich 1 soat ichida bo'lmasa — bir marta eslatma SMS."""
    minutes = cfg.get_int(db, "qr_reminder_minutes")
    now = clock.now()
    n = 0
    for b in db.scalars(
        select(Booking).where(
            Booking.status == BookingStatus.ACTIVE,
            Booking.smartphoneless.is_(False),
            Booking.qr_confirmed_at.is_(None),
            Booking.qr_reminder_sent_at.is_(None),
            Booking.arrival_at.is_not(None),
        )
    ):
        start = max(x for x in (b.stage1_at, b.arrival_at) if x is not None)
        if now - start >= timedelta(minutes=minutes):
            notify.send_sms(
                db,
                b.guest.phone,
                t("sms.qr_reminder", bookings.guest_lang(b), hotel=cfg.get(db, "hotel_name")),
                "qr_reminder",
                b.id,
            )
            b.qr_reminder_sent_at = now
            n += 1
    return n


def r6_not_full(db: Session) -> None:
    """R6: bron 24 soat ichida 3/3 ga yetmadi (smartfonsiz belgisi yo'q)."""
    cutoff = clock.now() - timedelta(hours=24)
    for b in db.scalars(
        select(Booking).where(
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.ACTIVE, BookingStatus.CHECKED_OUT]),
            Booking.smartphoneless.is_(False),
            Booking.created_at <= cutoff,
            Booking.created_at > cutoff - timedelta(days=3),
            Booking.stage < 3,
        )
    ):
        alerts.raise_alert(
            db,
            "R6",
            f"Bron #{b.id} ({b.room.number}) 24 soatda 3/3 ga yetmadi: hozir {b.stage}/3.",
            booking=b,
            dedupe_key=f"R6:{b.id}",
        )


def r8_underpaid(db: Session) -> None:
    """R8: qisman to'lov qilingan, lekin summa tarifdan kam va vaqt o'tdi."""
    grace = timedelta(minutes=cfg.get_int(db, "underpaid_grace_minutes"))
    now = clock.now()
    for b in db.scalars(select(Booking).where(Booking.status.in_([BookingStatus.PENDING, BookingStatus.ACTIVE]))):
        paid = b.paid_amount
        if paid <= 0 or paid >= b.final_amount or b.pay_later_approved:
            continue
        last = max(p.paid_at or p.created_at for p in b.payments)
        if now - last >= grace:
            alerts.raise_alert(
                db,
                "R8",
                f"Bron #{b.id} ({b.room.number}): to'langan {fmt_sum(paid)}, rasmiy summa {fmt_sum(b.final_amount)} "
                f"(kam: {fmt_sum(b.final_amount - paid)}).",
                booking=b,
                dedupe_key=f"R8:{b.id}:{b.final_amount}",
            )


def run_minutely(db: Session) -> None:
    steps = [
        discounts.expire_overdue,
        entry.check_edge,
        entry.resolve_suspects,
        entry.sweep_windows,
        qr_reminders,
        r8_underpaid,
        housekeeping.check_morning_missing,
        alerts.retry_unsent,
    ]
    for step in steps:
        try:
            with db.begin_nested():
                step(db)
        except Exception:  # pragma: no cover
            log.exception("Qoida qadamida xato: %s", step.__name__)


def run_every_15(db: Session) -> None:
    r6_not_full(db)
    entry.check_camera_silence(db)
