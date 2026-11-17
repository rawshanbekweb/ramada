"""O'qitish va namoyish uchun demo ma'lumotlar.

    python -m app.cli demo --days 14

Bo'sh (faqat `seed-demo` qilingan) bazaga o'tgan N kunlik real ko'rinishdagi tarix
yoziladi: xodimlar, smenalar, bronlar, to'lovlar, kamera hodisalari, QR tasdiqlari,
farrosh tekshiruvlari va buzilish holatlari (boshqa xona, kam to'lov, kassa farqi,
bronsiz xona). Barcha amallar haqiqiy servislar orqali bajariladi — panel, hisobotlar
va ogohlantirishlar xuddi pilotdagidek ko'rinadi.

HAQIQIY bazada ishlatmang: bron bor bazada buyruq rad etiladi.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.enums import BookingStatus, QrAnswer, Role
from app.integrations import sms as sms_mod
from app.integrations import telegram as tg
from app.models import Booking, Camera, CameraZone, Room, User
from app.security import make_room_token
from app.services import bookings, entry, guestflow, housekeeping, shifts
from app.services import settings_svc as cfg

FIRST = ["Aziz", "Dilnoza", "Rustam", "Malika", "Bekzod", "Gulnora", "Timur", "Zarina", "Sardor", "Nilufar",
         "Jasur", "Kamola", "Oybek", "Shahnoza", "Ilyos", "Madina", "Dmitriy", "Elena", "Ajiniyaz", "Gulbahar"]
LAST = ["Karimov", "Yusupova", "Aliyev", "Rashidova", "Tursunov", "Ismoilova", "Ivanov", "Petrova", "Utemuratov",
        "Kalliyeva", "Nazarov", "Saidova"]


class DemoError(Exception):
    pass


def _door_event(db: Session, room: Room, at: datetime, strong: bool = True) -> None:
    z = db.scalar(select(CameraZone).where(CameraZone.room_id == room.id, CameraZone.kind == "door"))
    if z is None:
        return
    cam = db.get(Camera, z.camera_id)
    entry.ingest_event(
        db, channel=cam.channel, region_id=z.region_id, event_type="dwell" if strong else "enter",
        occurred_at=at, edge_received_at=at,
    )


def generate(db: Session, days: int = 14, seed: int = 7) -> dict:
    if db.scalar(select(Booking.id)):
        raise DemoError("Bazada bronlar bor — demo faqat bo'sh bazaga yoziladi")
    rnd = random.Random(seed)
    # Demo paytida tashqariga hech narsa yuborilmaydi
    tg.set_backend(tg.FakeTelegram())
    sms_mod.set_provider(sms_mod.ConsoleSms())
    cfg.state_set(db, "demo_data", True)

    staff = {
        "rec1": User(full_name="Dilnoza Reception (demo)", role=Role.RECEPTION),
        "rec2": User(full_name="Bahodir Reception (demo)", role=Role.RECEPTION),
        "admin": User(full_name="Admin (demo)", role=Role.ADMIN),
        "hk": User(full_name="Gulshan Farrosh (demo)", role=Role.HOUSEKEEPER),
    }
    db.add_all(staff.values())
    db.flush()
    rooms = list(db.scalars(select(Room).where(Room.active.is_(True)).order_by(Room.number)))
    stats = {"bookings": 0, "violations": 0}
    start_day = clock.today() - timedelta(days=days)
    last_counted = 0
    try:
        for d in range(days):
            day = start_day + timedelta(days=d)
            clock.set_now(clock.local_dt(day, 8))
            # Ertalab: farrosh tekshiruvi
            marks = {r.id: housekeeping.occupied_overnight(db, r.id, day) is not None for r in rooms}
            if d == days // 2:
                ghost = rnd.choice([r for r in rooms if not marks[r.id]])
                marks[ghost.id] = True  # bronsiz, lekin ishlatilgan xona (R7)
                stats["violations"] += 1
            housekeeping.save_morning_check(db, staff["hk"], marks, day)
            # Kechagi mehmonlarni chiqarish
            for b in list(db.scalars(select(Booking).where(Booking.status == BookingStatus.ACTIVE))):
                if b.checkout_date <= day:
                    clock.set_now(clock.local_dt(day, rnd.choice([9, 10, 11])))
                    bookings.checkout(db, b, staff["admin"], force=True)
                    housekeeping.start_cleaning(db, staff["hk"], b.room_id)
                    clock.advance(minutes=25)
                    housekeeping.finish_cleaning(db, staff["hk"])
            # Kunduzgi smena
            rec = staff["rec1"] if d % 2 == 0 else staff["rec2"]
            clock.set_now(clock.local_dt(day, 12))
            shifts.open_shift(db, rec, last_counted)
            free = bookings.available_rooms(db, day, day + timedelta(days=1), require_clean=False)
            n_new = min(len(free), rnd.randint(8, 14))
            for i, room in enumerate(rnd.sample(free, n_new)):
                clock.set_now(clock.local_dt(day, 13) + timedelta(minutes=i * 25))
                room.state = "clean"
                phoneless = rnd.random() < (0.25 if rec is staff["rec2"] else 0.07)
                disc = 0
                if rnd.random() < 0.08:
                    disc = 50_000
                b = bookings.create_booking(
                    db, user=rec, room_id=room.id, nights=rnd.choice([1, 1, 2, 2, 3]),
                    guest_name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}", phone=f"+99890{rnd.randint(1000000, 9999999)}",
                    photo_tg="demo", smartphoneless=phoneless, discount_amount=disc,
                    discount_reason="doimiy mijoz" if disc else None,
                )
                stats["bookings"] += 1
                if disc:
                    from app.models import Discount
                    from app.services import discounts

                    discounts.decide(db, db.scalar(select(Discount).where(Discount.booking_id == b.id)), staff["admin"], True)
                dev = f"demo-dev-{b.id}"
                if not phoneless:
                    clock.advance(minutes=2)
                    bookings.guest_accept(db, b, _dh(dev))
                clock.advance(minutes=1)
                amount = b.final_amount
                underpay = i == 3 and d == days - 3
                if underpay:
                    amount -= 100_000  # kam to'lov (R8)
                    stats["violations"] += 1
                bookings.record_payment(db, b, rnd.choice(["cash", "cash", "terminal"]), amount, user=rec,
                                        receipt_no=f"{rnd.randint(100000, 999999)}")
                if underpay:
                    clock.advance(minutes=20)
                    from app.services import rules

                    rules.r8_underpaid(db)
                    bookings.record_payment(db, b, "cash", 100_000, user=rec, receipt_no=f"{rnd.randint(100000, 999999)}")
                if b.stage < 1:
                    continue
                bookings.issue_key(db, b, rec)
                # Kamera: ko'pchilik o'z xonasiga boradi; ba'zan boshqa xonaga (R3)
                walk = clock.now() + timedelta(minutes=rnd.randint(2, 8))
                if i == 1 and d in (4, days - 2):
                    other = rnd.choice([r for r in rooms if bookings.current_booking(db, r.id) is None and r.id != room.id])
                    _door_event(db, other, walk)
                    stats["violations"] += 1
                if rnd.random() < 0.96:
                    _door_event(db, room, walk + timedelta(seconds=40))
                # QR: smartfonli mehmonlarning ko'pchiligi skanerlaydi
                if not phoneless and rnd.random() < 0.88:
                    clock.set_now(walk + timedelta(minutes=rnd.randint(5, 40)))
                    res = guestflow.scan(db, make_room_token(room.id, room.qr_version), _dh(dev), None)
                    if res.kind == "question":
                        ans = QrAnswer.OTHER_AMOUNT if (d == days - 4 and i == 2) else QrAnswer.YES
                        if ans != QrAnswer.YES:
                            stats["violations"] += 1
                        guestflow.answer(db, res.scan.id, ans, _dh(dev), claimed_amount=b.final_amount + 150_000 if ans != QrAnswer.YES else None)
            # Kechqurun: bronsiz xonaga qisqa kirish (bir marta) va smenani yopish
            clock.set_now(clock.local_dt(day, 22))
            from app.services import rules

            rules.run_minutely(db)
            if d == days // 3:
                empty = [r for r in rooms if bookings.current_booking(db, r.id) is None]
                _door_event(db, rnd.choice(empty), clock.now())
                stats["violations"] += 1
            sh = shifts.current_shift(db, rec)
            # Kechqurun administrator tushumni oladi, kassada 300 000 qoladi (inkassatsiya)
            take = shifts.expected_cash(db, sh) - 300_000
            if take > 0:
                shifts.withdraw(db, staff["admin"], sh, take, "kunlik inkassatsiya")
            expected = shifts.expected_cash(db, sh)
            short = 50_000 if d == days - 5 else 0
            if short:
                stats["violations"] += 1
            shifts.close_shift(db, rec, expected - short)
            last_counted = expected - short
            clock.advance(hours=1)
            entry.sweep_windows(db)
            db.flush()
    finally:
        clock.set_now(None)
        tg.set_backend(None)
        sms_mod.set_provider(None)
    entry.heartbeat(db, {"nvr_ok": True, "queue": 0, "channels": {str(c.channel): True for c in db.scalars(select(Camera))}})
    return stats


def _dh(dev: str) -> str:
    from app.security import device_hash

    return device_hash(dev)
