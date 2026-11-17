"""Ogohlantirishlarni yaratish (TT 8-bo'lim, R1–R12)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.enums import AlertStatus, Severity
from app.models import Alert, Booking, Room, User
from app.services import audit, notify

RULES: dict[str, tuple[str, str]] = {
    "R1": (Severity.MEDIUM, "To'lov bor, kirish oynasida shu xona eshigida harakat yo'q"),
    "R2": (Severity.HIGH, "Bronsiz yoki bo'sh xona eshigida harakat (farrosh belgisi yo'q)"),
    "R3": (Severity.CRITICAL, "To'langan xonaga emas, boshqa xonaga kirildi"),
    "R4": (Severity.HIGH, "Xona QR'i faol broni yo'q xonadan yoki boshqa xona mehmoni tomonidan skanerlandi"),
    "R5": (Severity.CRITICAL, "Mehmon «boshqa summa to'ladim» yoki «boshqa xonadaman» dedi"),
    "R6": (Severity.MEDIUM, "Bron 24 soat ichida 3/3 ga yetmadi"),
    "R7": (Severity.HIGH, "Farrosh «ishlatilgan» dedi, bron yo'q"),
    "R8": (Severity.HIGH, "To'langan summa tarifdan kam"),
    "R9": (Severity.HIGH, "Smena kassasida farq"),
    "R10": (Severity.HIGH, "Bron to'lovdan keyin bekor qilindi"),
    "R11": (Severity.MEDIUM, "Xodim ko'rsatkichlari boshqalardan sezilarli farq qiladi"),
    "R12": (Severity.MEDIUM, "NVR aloqasi / ertalabki tekshiruv / QR plita muammosi"),
    # TT 16-bo'lim, 1-sinov: tasdiqsiz kalit berishga urinish — egaga xabar
    "KEY": (Severity.HIGH, "Tasdiq va to'lovsiz kalit berishga urinish"),
    # FT-26: chiqishdagi so'rovnomada narx bo'yicha shikoyat
    "PRICE": (Severity.HIGH, "Mehmondan narx bo'yicha shikoyat"),
}

URGENT = (Severity.HIGH, Severity.CRITICAL)


def raise_alert(
    db: Session,
    rule: str,
    message: str,
    *,
    severity: str | None = None,
    room: Room | None = None,
    booking: Booking | None = None,
    user: User | None = None,
    user_id: int | None = None,
    shift_id: int | None = None,
    zone_event=None,
    video_link: str | None = None,
    dedupe_key: str | None = None,
    data: dict | None = None,
    at=None,
) -> Alert | None:
    """Ogohlantirish yaratadi. `dedupe_key` takrorlansa — None qaytaradi."""
    if dedupe_key and db.scalar(select(Alert.id).where(Alert.dedupe_key == dedupe_key)):
        return None
    severity = severity or RULES[rule][0]
    if booking is not None and room is None:
        room = booking.room
    if booking is not None and user is None and user_id is None:
        user_id = booking.created_by_id
    if booking is not None and shift_id is None:
        shift_id = booking.shift_id
    alert = Alert(
        rule=rule,
        severity=severity,
        message=message,
        room_id=room.id if room else None,
        booking_id=booking.id if booking else None,
        user_id=user.id if user else user_id,
        shift_id=shift_id,
        zone_event_id=zone_event.id if zone_event is not None else None,
        video_link=video_link or (zone_event.video_link if zone_event is not None else None),
        dedupe_key=dedupe_key,
        data=data or {},
        at=at or clock.now(),
        status=AlertStatus.NEW,
    )
    db.add(alert)
    db.flush()
    audit.log(db, "system", "alert.raise", alert, new={"rule": rule, "severity": severity, "message": message})
    if severity in URGENT:
        notify.later_alert(db, alert.id)
    return alert


def set_status(db: Session, alert: Alert, status: str, user: User | None, note: str | None = None) -> None:
    old = alert.status
    alert.status = status
    if note:
        alert.note = note
    if user is not None:
        alert.resolved_by_id = user.id
    audit.log(db, audit.actor_of(user), "alert.status", alert, old={"status": old}, new={"status": status, "note": note})


def retry_unsent(db: Session) -> int:
    """Telegram vaqtincha ishlamagan bo'lsa — yuborilmagan yuqori/kritik ogohlantirishlarni qayta yuborish."""
    rows = db.scalars(
        select(Alert).where(Alert.notified_at.is_(None), Alert.severity.in_(list(URGENT))).order_by(Alert.id)
    ).all()
    for a in rows:
        notify.dispatch_alert(db, a)
    return len(rows)
