"""Mehmon sahifasi va xona QR tasdig'i (1- va 3-bosqich, FT-20..FT-26, R4, R5)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.enums import BookingStatus, ConfirmSource, QrAnswer
from app.models import Booking, QrScan, Room, Survey
from app.security import parse_room_token
from app.services import alerts, audit, bookings, notify
from app.services.pricing import fmt_sum

MAX_LAST4_ATTEMPTS = 5


class ScanResult:
    """QR skanerlash natijasi (sahifa nimani ko'rsatishi kerak)."""

    def __init__(self, kind: str, room: Room | None = None, booking: Booking | None = None, scan: QrScan | None = None, **extra):
        self.kind = kind  # bad_code / need_last4 / too_many / no_booking / mismatch / not_paid / question / already
        self.room = room
        self.booking = booking
        self.scan = scan
        self.extra = extra


def booking_by_token(db: Session, token: str) -> Booking | None:
    return db.scalar(select(Booking).where(Booking.guest_token == token))


def link_valid(booking: Booking) -> bool:
    return clock.now() <= booking.guest_token_expires_at


def device_allowed(booking: Booking, device_hash: str | None) -> bool:
    """Havola bir martalik: birinchi tasdiqlagan qurilmaga bog'lanadi."""
    return booking.guest_device_hash is None or booking.guest_device_hash == device_hash


def phone_last4_ok(booking: Booking, last4: str) -> bool:
    digits = "".join(ch for ch in booking.guest.phone if ch.isdigit())
    return len(last4) == 4 and last4.isdigit() and digits.endswith(last4)


def _attempts(db: Session, room_id: int, device_hash: str | None, ip: str | None) -> int:
    since = clock.now() - timedelta(hours=1)
    q = select(func.count(QrScan.id)).where(
        QrScan.room_id == room_id, QrScan.at >= since, QrScan.outcome == "last4_wrong"
    )
    if device_hash:
        q = q.where(QrScan.device_hash == device_hash)
    elif ip:
        q = q.where(QrScan.ip == ip)
    return db.scalar(q) or 0


def resolve_room(db: Session, token: str) -> Room | None:
    parsed = parse_room_token(token)
    if parsed is None:
        return None
    room = db.get(Room, parsed[0])
    if room is None or room.qr_version != parsed[1] or not room.active:
        return None
    return room


def scan(db: Session, token: str, device_hash: str | None, ip: str | None, last4: str | None = None) -> ScanResult:
    room = resolve_room(db, token)
    if room is None:
        return ScanResult("bad_code")
    now = clock.now()
    rec = QrScan(room_id=room.id, at=now, device_hash=device_hash, ip=ip)
    db.add(rec)
    room_booking = bookings.current_booking(db, room.id)
    pending_room_booking = db.scalars(
        select(Booking).where(Booking.room_id == room.id, Booking.status == BookingStatus.PENDING)
    ).first()
    device_booking = bookings.find_active_by_device(db, device_hash)

    # Qurilma boshqa xonaning mehmoniga tegishli — QR boshqa xonada skanerlandi
    if device_booking is not None and device_booking.room_id != room.id:
        rec.method, rec.found_booking_id, rec.outcome = "device", device_booking.id, "mismatch"
        db.flush()
        alerts.raise_alert(
            db,
            "R4",
            f"{room.number}-xona QR'i bron #{device_booking.id} ({device_booking.room.number}-xona) mehmoni "
            f"telefonidan skanerlandi. Boshqa xona berilgan yoki QR nusxalangan bo'lishi mumkin.",
            room=room,
            booking=device_booking,
            dedupe_key=f"R4:mismatch:{device_booking.id}:{room.id}",
            data={"qr_room": room.number, "booking_room": device_booking.room.number},
        )
        return ScanResult("mismatch", room, device_booking, rec)

    if room_booking is None:
        if pending_room_booking is not None and (
            device_booking is pending_room_booking
            or (last4 and phone_last4_ok(pending_room_booking, last4))
        ):
            rec.found_booking_id, rec.outcome = pending_room_booking.id, "not_paid"
            db.flush()
            return ScanResult("not_paid", room, pending_room_booking, rec)
        rec.outcome = "no_booking"
        db.flush()
        alerts.raise_alert(
            db,
            "R4",
            f"{room.number}-xona QR'i faol broni yo'q xonadan skanerlandi"
            + (f" (to'lanmagan bron #{pending_room_booking.id} bor)" if pending_room_booking else "")
            + ".",
            room=room,
            booking=pending_room_booking,
            dedupe_key=f"R4:empty:{room.id}:{clock.local(now):%Y%m%d%H}",
        )
        return ScanResult("no_booking", room, None, rec)

    # Xonada faol bron bor: mehmonni aniqlash
    if device_booking is not None and device_booking.id == room_booking.id:
        rec.method = "device"
    elif last4 is not None:
        if _attempts(db, room.id, device_hash, ip) >= MAX_LAST4_ATTEMPTS:
            rec.outcome = "too_many"
            db.flush()
            return ScanResult("too_many", room, None, rec)
        if phone_last4_ok(room_booking, last4):
            rec.method = "last4"
            if device_hash and room_booking.guest_device_hash is None:
                room_booking.guest_device_hash = device_hash
        else:
            # Raqam boshqa xonadagi faol bronga mos keladimi?
            other = _find_by_last4(db, last4, exclude_room=room.id)
            if other is not None:
                rec.method, rec.found_booking_id, rec.outcome = "last4", other.id, "mismatch"
                db.flush()
                alerts.raise_alert(
                    db,
                    "R4",
                    f"{room.number}-xona QR'i bron #{other.id} ({other.room.number}-xona) mehmoni tomonidan "
                    f"(telefon oxirgi 4 raqami bo'yicha) skanerlandi.",
                    room=room,
                    booking=other,
                    dedupe_key=f"R4:mismatch:{other.id}:{room.id}",
                )
                return ScanResult("mismatch", room, other, rec)
            rec.outcome = "last4_wrong"
            db.flush()
            return ScanResult("need_last4", room, None, rec, wrong=True)
    else:
        rec.outcome = "need_last4"
        db.flush()
        return ScanResult("need_last4", room, None, rec)

    rec.found_booking_id = room_booking.id
    if room_booking.qr_confirmed_at is not None:
        rec.outcome = "already"
        db.flush()
        return ScanResult("already", room, room_booking, rec)
    rec.outcome = "question"
    db.flush()
    return ScanResult("question", room, room_booking, rec)


def _find_by_last4(db: Session, last4: str, exclude_room: int) -> Booking | None:
    if not (last4.isdigit() and len(last4) == 4):
        return None
    for b in db.scalars(select(Booking).where(Booking.status == BookingStatus.ACTIVE, Booking.room_id != exclude_room)):
        if phone_last4_ok(b, last4):
            return b
    return None


def answer(
    db: Session,
    scan_id: int,
    answer_value: str,
    device_hash: str | None,
    claimed_amount: int | None = None,
    claimed_room: str | None = None,
) -> Booking | None:
    rec = db.get(QrScan, scan_id)
    if rec is None or rec.found_booking_id is None or rec.answer is not None:
        return None
    if rec.device_hash and device_hash != rec.device_hash:
        return None
    if clock.now() - rec.at > timedelta(minutes=30):
        return None
    booking = db.get(Booking, rec.found_booking_id)
    if booking is None or booking.status != BookingStatus.ACTIVE or booking.room_id != rec.room_id:
        return None
    rec.answer = answer_value
    rec.answered_at = clock.now()
    audit.log(db, "guest", "qr.answer", rec, new={"answer": answer_value, "amount": claimed_amount, "room": claimed_room})
    if answer_value == QrAnswer.YES:
        booking.qr_confirmed_at = clock.now()
        if device_hash and booking.guest_device_hash is None:
            booking.guest_device_hash = device_hash
        bookings.add_confirmation(
            db, booking, 3, ConfirmSource.QR, {"qr_scan_id": rec.id, "device": (device_hash or "")[:12]}
        )
    else:
        what = "boshqa summa to'laganini" if answer_value == QrAnswer.OTHER_AMOUNT else "boshqa xonada ekanini"
        details = []
        if claimed_amount:
            details.append(f"mehmon aytgan summa: {fmt_sum(claimed_amount)}")
        if claimed_room:
            details.append(f"mehmon aytgan xona: {claimed_room}")
        alerts.raise_alert(
            db,
            "R5",
            f"Bron #{booking.id} ({booking.room.number}-xona) mehmoni {what} bildirdi. "
            f"Tizimdagi to'lov: {fmt_sum(booking.paid_amount)}. " + "; ".join(details),
            booking=booking,
            dedupe_key=f"R5:{booking.id}:{answer_value}",
            data={"answer": answer_value, "claimed_amount": claimed_amount, "claimed_room": claimed_room},
        )
    return booking


def submit_survey(
    db: Session, booking: Booking, rating: int | None, price_complaint: bool, paid_claim: int | None, comment: str | None
) -> Survey:
    s = db.scalar(select(Survey).where(Survey.booking_id == booking.id))
    if s is not None:
        return s
    s = Survey(
        booking_id=booking.id,
        rating=rating if rating and 1 <= rating <= 5 else None,
        price_complaint=price_complaint,
        paid_amount_claimed=paid_claim,
        comment=(comment or "").strip()[:2000] or None,
        at=clock.now(),
    )
    db.add(s)
    db.flush()
    audit.log(db, "guest", "survey.submit", s, new={"rating": s.rating, "complaint": price_complaint})
    text = (
        f"📝 So'rovnoma: bron #{booking.id} ({booking.room.number}-xona), baho: {s.rating or '—'}"
        + (f"\nIzoh: {s.comment}" if s.comment else "")
    )
    if price_complaint:
        alerts.raise_alert(
            db,
            "PRICE",
            f"Mehmon narx bo'yicha shikoyat qildi: bron #{booking.id}, tizimda {fmt_sum(booking.paid_amount)}, "
            f"mehmon aytgani: {fmt_sum(paid_claim) if paid_claim else '—'}. {s.comment or ''}",
            booking=booking,
            dedupe_key=f"PRICE:{booking.id}",
        )
    else:
        for o in notify.owners(db):
            notify.later(db, o.telegram_id, text)
    return s
