import base64
from datetime import timedelta

from sqlalchemy import select

from app import clock
from app.models import Booking, Room
from app.services import bookings, entry, shifts


def room(db, number) -> Room:
    return db.scalar(select(Room).where(Room.number == number))


def open_shift(db, user, cash=0):
    if shifts.current_shift(db, user) is None:
        shifts.open_shift(db, user, cash)
        db.commit()


def new_booking(db, user, number="101", nights=2, **kw) -> Booking:
    open_shift(db, user)
    kw.setdefault("guest_name", "Mehmon Testov")
    kw.setdefault("phone", "+998901234567")
    kw.setdefault("photo_tg", "file-id")
    b = bookings.create_booking(db, user=user, room_id=room(db, number).id, nights=nights, **kw)
    db.commit()
    return b


def accept_and_pay_cash(db, b, user, amount=None, receipt="CHK-1", device=None):
    bookings.guest_accept(db, b, device or f"dev-{b.id}")
    bookings.record_payment(db, b, "cash", amount or b.final_amount, user=user, receipt_no=receipt)
    db.commit()


def door_event(db, number, minutes_from_now=1, eid=None):
    r = room(db, number)
    floor = r.floor
    region = int(number[1:])
    at = clock.now() + timedelta(minutes=minutes_from_now)
    ev = entry.ingest_event(db, channel=floor, region_id=region, event_type="regionEntrance",
                            occurred_at=at, edge_event_id=eid)
    db.commit()
    return ev


def heartbeat(db, nvr_ok=True):
    entry.heartbeat(db, {"nvr_ok": nvr_ok})
    db.commit()


def payme_auth():
    return "Basic " + base64.b64encode(b"Paycom:payme-test-key").decode()
