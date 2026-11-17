"""Farrosh: ertalabki tekshiruv, tozalash, QR plita, choyshab (FT-40, FT-47, R7, R12)."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.enums import BookingStatus, ConfirmSource, Role, RoomState
from app.models import Alert, Booking, CleaningSession, LinenLog, Occupancy, Room, RoomCheck, User
from app.services import alerts, audit, bookings


class HousekeepingError(Exception):
    pass


def rooms(db: Session) -> list[Room]:
    return list(db.scalars(select(Room).where(Room.active.is_(True)).order_by(Room.floor, Room.number)))


def night_window(db: Session, day: date):
    """`day` ertalabidagi tekshiruv oldingi kecha haqida: (kecha 18:00, bugun 10:00)."""
    return clock.local_dt(day - timedelta(days=1), 18), clock.local_dt(day, 10)


def occupied_overnight(db: Session, room_id: int, day: date) -> Booking | None:
    start, end = night_window(db, day)
    occ = db.scalars(
        select(Occupancy).where(
            Occupancy.room_id == room_id,
            Occupancy.start_at < end,
            (Occupancy.end_at.is_(None)) | (Occupancy.end_at > start),
        )
    ).first()
    return db.get(Booking, occ.booking_id) if occ else None


def save_morning_check(db: Session, user: User, marks: dict[int, bool], day: date | None = None) -> list[Alert]:
    """marks: {room_id: ishlatilgan?}. Bronsiz, lekin ishlatilgan xona — R7."""
    if user.role not in (Role.HOUSEKEEPER, Role.ADMIN, Role.OWNER):
        raise HousekeepingError("Huquq yo'q")
    day = day or clock.today()
    raised = []
    for room_id, used in marks.items():
        row = db.scalar(select(RoomCheck).where(RoomCheck.day == day, RoomCheck.room_id == room_id, RoomCheck.kind == "morning"))
        if row is None:
            row = RoomCheck(day=day, room_id=room_id, user_id=user.id, kind="morning", used=used, at=clock.now())
            db.add(row)
        else:
            row.used, row.user_id, row.at = used, user.id, clock.now()
        room = db.get(Room, room_id)
        booking = occupied_overnight(db, room_id, day)
        if used and booking is None:
            since = clock.now() - timedelta(hours=24)
            had_r2 = db.scalar(
                select(Alert.id).where(Alert.rule.in_(["R2", "R3"]), Alert.room_id == room_id, Alert.at >= since)
            )
            a = alerts.raise_alert(
                db,
                "R7",
                f"Farrosh {user.full_name}: {room.number}-xona kechasi ishlatilgan, lekin bron yo'q"
                + (" — kamera ham bronsiz harakatni qayd etgan (R2)." if had_r2 else "."),
                severity="critical" if had_r2 else "high",
                room=room,
                user=user,
                dedupe_key=f"R7:{room_id}:{day.isoformat()}",
            )
            if a:
                raised.append(a)
        elif used and booking is not None and booking.smartphoneless and booking.stage == 2:
            # 6.6: smartfonsiz mehmon uchun 3-bosqich o'rniga ertasi kuni farrosh tasdig'i
            bookings.add_confirmation(
                db, booking, 3, ConfirmSource.HOUSEKEEPER, {"room_check_day": day.isoformat(), "user": user.id}
            )
    audit.log(db, f"user:{user.id}", "housekeeping.morning", object_type="room_checks", object_id=day.isoformat(),
              new={"used": [k for k, v in marks.items() if v]})
    return raised


def morning_done(db: Session, day: date) -> set[int]:
    return set(db.scalars(select(RoomCheck.room_id).where(RoomCheck.day == day, RoomCheck.kind == "morning")))


def check_morning_missing(db: Session) -> None:
    """R12: soat 10:00 gacha ertalabki tekshiruv o'tkazilmadi."""
    from app.services import settings_svc as cfg

    now_local = clock.local()
    if now_local.hour < cfg.get_int(db, "morning_check_hour"):
        return
    day = now_local.date()
    done = morning_done(db, day)
    missing = [r.number for r in rooms(db) if r.id not in done]
    if missing:
        alerts.raise_alert(
            db,
            "R12",
            f"Ertalabki xona tekshiruvi {cfg.get_int(db, 'morning_check_hour')}:00 gacha o'tkazilmadi: "
            f"{len(missing)} xona ({', '.join(missing[:15])}{'…' if len(missing) > 15 else ''}).",
            dedupe_key=f"R12:morning:{day.isoformat()}",
            data={"kind": "morning", "missing": missing},
        )


def start_cleaning(db: Session, user: User, room_id: int) -> CleaningSession:
    room = db.get(Room, room_id)
    if room is None:
        raise HousekeepingError("Xona topilmadi")
    open_s = db.scalar(
        select(CleaningSession).where(CleaningSession.user_id == user.id, CleaningSession.finished_at.is_(None))
    )
    if open_s is not None:
        open_s.finished_at = clock.now()
    s = CleaningSession(room_id=room_id, user_id=user.id, started_at=clock.now())
    db.add(s)
    if room.state != RoomState.OUT_OF_ORDER:
        room.state = RoomState.CLEANING
    db.flush()
    audit.log(db, f"user:{user.id}", "cleaning.start", s, new={"room": room.number})
    return s


def finish_cleaning(db: Session, user: User, session_id: int | None = None) -> CleaningSession | None:
    q = select(CleaningSession).where(CleaningSession.user_id == user.id, CleaningSession.finished_at.is_(None))
    if session_id:
        q = q.where(CleaningSession.id == session_id)
    s = db.scalars(q.order_by(CleaningSession.id.desc())).first()
    if s is None:
        return None
    s.finished_at = clock.now()
    room = db.get(Room, s.room_id)
    occupied = bookings.current_booking(db, room.id)
    if room.state == RoomState.CLEANING:
        room.state = RoomState.CLEAN
    audit.log(db, f"user:{user.id}", "cleaning.finish", s, new={"room": room.number, "occupied": bool(occupied)})
    return s


def open_cleaning(db: Session, user: User) -> CleaningSession | None:
    return db.scalar(
        select(CleaningSession).where(CleaningSession.user_id == user.id, CleaningSession.finished_at.is_(None))
    )


def save_qr_plate_check(db: Session, user: User, marks: dict[int, bool], day: date | None = None) -> None:
    """Haftalik QR plita tekshiruvi; plita yo'q/shikastlangan — R12."""
    day = day or clock.today()
    for room_id, ok in marks.items():
        row = db.scalar(select(RoomCheck).where(RoomCheck.day == day, RoomCheck.room_id == room_id, RoomCheck.kind == "qr_plate"))
        if row is None:
            db.add(RoomCheck(day=day, room_id=room_id, user_id=user.id, kind="qr_plate", qr_ok=ok, at=clock.now()))
        else:
            row.qr_ok = ok
        if not ok:
            room = db.get(Room, room_id)
            alerts.raise_alert(
                db,
                "R12",
                f"{room.number}-xonada QR plita yo'q yoki shikastlangan (farrosh {user.full_name}).",
                room=room,
                user=user,
                dedupe_key=f"R12:qr:{room_id}:{day.isoformat()}",
                data={"kind": "qr_plate"},
            )
    audit.log(db, f"user:{user.id}", "housekeeping.qr_plates", object_type="room_checks", object_id=day.isoformat(),
              new={"bad": [k for k, v in marks.items() if not v]})


def last_qr_check(db: Session) -> date | None:
    return db.scalar(select(RoomCheck.day).where(RoomCheck.kind == "qr_plate").order_by(RoomCheck.day.desc()))


def log_linen(db: Session, user: User, sent: int, returned: int, day: date | None = None) -> LinenLog:
    row = LinenLog(day=day or clock.today(), sent=sent, returned=returned, user_id=user.id, at=clock.now())
    db.add(row)
    db.flush()
    audit.log(db, f"user:{user.id}", "linen.log", row, new={"sent": sent, "returned": returned})
    return row


def linen_balance(db: Session, start: date, end: date) -> dict:
    """FT-47: kir yuvishga berilgan to'plamlar ↔ chiqishlar va tozalashlar soni."""
    logs = db.scalars(select(LinenLog).where(LinenLog.day >= start, LinenLog.day < end)).all()
    sent = sum(x.sent for x in logs)
    returned = sum(x.returned for x in logs)
    s_dt, e_dt = clock.local_dt(start), clock.local_dt(end)
    checkouts = len(
        db.scalars(
            select(Booking.id).where(
                Booking.status == BookingStatus.CHECKED_OUT, Booking.checked_out_at >= s_dt, Booking.checked_out_at < e_dt
            )
        ).all()
    )
    cleanings = len(
        db.scalars(select(CleaningSession.id).where(CleaningSession.started_at >= s_dt, CleaningSession.started_at < e_dt)).all()
    )
    return {"sent": sent, "returned": returned, "checkouts": checkouts, "cleanings": cleanings, "diff": sent - checkouts}
