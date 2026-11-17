"""TT 16-bo'lim: qabul sinovlari (1–12)."""
from datetime import timedelta

from sqlalchemy import select

from app import clock
from app.enums import BookingStatus, QrAnswer
from app.models import Alert, Booking
from app.security import make_room_token
from app.services import bookings, entry, guestflow, rules, shifts
from tests.helpers import accept_and_pay_cash, door_event, heartbeat, new_booking, room


def alerts_of(db, rule):
    return db.scalars(select(Alert).where(Alert.rule == rule)).all()


def test_01_key_without_confirmation_and_payment(db, staff, telegram):
    """1. Mehmon tasdig'i va to'lovsiz kalit berishga urinish → 1/3 ga o'tmaydi, egaga xabar."""
    rec = staff["reception"]
    b = new_booking(db, rec)
    assert bookings.issue_key(db, b, rec) is False
    db.commit()
    assert b.stage == 0 and b.status == BookingStatus.PENDING
    assert alerts_of(db, "KEY")
    assert any(m["chat_id"] == 1001 and "KEY" in m["text"] for m in telegram.sent)
    # To'lov bor, lekin mehmon tasdiqlamagan — baribir 1/3 emas
    bookings.record_payment(db, b, "cash", b.final_amount, user=rec, receipt_no="F-1")
    db.commit()
    assert b.stage == 0
    bookings.guest_accept(db, b, "d1")
    db.commit()
    assert b.stage == 1 and bookings.issue_key(db, b, rec) is True


def test_02_underpayment(db, staff):
    """2. Tarifdan past summa → sahifada rasmiy narx; to'lov kam bo'lsa bron yopilmaydi (R8)."""
    rec = staff["reception"]
    b = new_booking(db, rec, nights=2)
    official = b.official_amount
    assert official == 1_000_000  # 2 tun × 500 000 (Standart, ish kuni)
    bookings.guest_accept(db, b, "d2")
    bookings.record_payment(db, b, "cash", 700_000, user=rec, receipt_no="F-2")
    db.commit()
    assert b.stage == 0 and b.status == BookingStatus.PENDING
    clock.advance(minutes=20)
    rules.r8_underpaid(db)
    db.commit()
    assert alerts_of(db, "R8")


def test_03_entry_confirmed_by_camera(db, staff):
    """3. To'lovdan keyin to'langan xona eshigiga borish → ≤ 2 daqiqada 2/3, video havola bilan."""
    rec = staff["reception"]
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec)
    assert b.stage == 1
    door_event(db, "101", minutes_from_now=1)
    db.refresh(b)
    assert b.stage == 2
    conf = [c for c in b.confirmations if c.stage == 2][0]
    assert conf.evidence["video"].startswith("https://hotel.test/panel/events/")


def test_04_no_entry_in_window(db, staff):
    """4. To'lovdan keyin 30 daqiqa hech qayerga bormaslik → R1."""
    rec = staff["reception"]
    b = new_booking(db, rec)
    accept_and_pay_cash(db, b, rec)
    clock.advance(minutes=31)
    heartbeat(db)
    entry.sweep_windows(db)
    db.commit()
    a = alerts_of(db, "R1")
    assert len(a) == 1 and a[0].severity == "medium" and a[0].booking_id == b.id


def test_05_paid_101_entered_204(db, staff, telegram):
    """5. 101 uchun to'lab, 204 eshigiga borish → R3 kritik."""
    rec = staff["reception"]
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec)
    door_event(db, "204", minutes_from_now=2)
    a = alerts_of(db, "R3")
    assert len(a) == 1 and a[0].severity == "critical"
    assert "101" in a[0].message and "204" in a[0].message
    assert any("R3" in m["text"] for m in telegram.sent if m["chat_id"] == 1001)
    assert not alerts_of(db, "R2")  # R3 R2 o'rnini bosadi


def test_06_qr_yes(db, staff):
    """6. Xona QR'ini skanerlab «Ha» bosish → 3/3, Wi-Fi paroli ko'rinadi."""
    rec = staff["reception"]
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec, device="phone-A")
    door_event(db, "101")
    token = make_room_token(room(db, "101").id, 1)
    res = guestflow.scan(db, token, "phone-A", "1.1.1.1")
    assert res.kind == "question" and res.booking.id == b.id
    guestflow.answer(db, res.scan.id, QrAnswer.YES, "phone-A")
    db.commit()
    db.refresh(b)
    assert b.stage == 3 and b.qr_confirmed_at is not None


def test_07_qr_other_amount(db, staff, telegram):
    """7. QR'da «boshqa summa to'ladim» → R5 kritik, egaga ≤ 1 daqiqa."""
    rec = staff["reception"]
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec, device="phone-B")
    token = make_room_token(room(db, "101").id, 1)
    res = guestflow.scan(db, token, "phone-B", None)
    guestflow.answer(db, res.scan.id, QrAnswer.OTHER_AMOUNT, "phone-B", claimed_amount=1_200_000)
    db.commit()
    a = alerts_of(db, "R5")
    assert len(a) == 1 and a[0].severity == "critical" and a[0].notified_at is not None
    assert any("R5" in m["text"] for m in telegram.sent if m["chat_id"] == 1001)


def test_08_empty_room_qr(db, staff):
    """8. Bo'sh xona QR'ini skanerlash → R4."""
    token = make_room_token(room(db, "305").id, 1)
    res = guestflow.scan(db, token, "stranger", None)
    db.commit()
    assert res.kind == "no_booking"
    assert alerts_of(db, "R4")


def test_09_qr_photo_in_other_room(db, staff):
    """9. QR rasmini boshqa xonada skanerlash → bron topilmaydi yoki R4."""
    rec = staff["reception"]
    b101 = new_booking(db, rec, number="101", phone="+998901111111")
    accept_and_pay_cash(db, b101, rec, device="phone-101")
    b204 = new_booking(db, rec, number="204", phone="+998902222222")
    accept_and_pay_cash(db, b204, rec, device="phone-204")
    # 204 mehmoni 101 QR'ining suratini skanerladi
    token101 = make_room_token(room(db, "101").id, 1)
    res = guestflow.scan(db, token101, "phone-204", None)
    db.commit()
    assert res.kind == "mismatch"
    assert any(a.booking_id == b204.id for a in alerts_of(db, "R4"))
    # Notanish qurilma + 204 telefonining oxirgi 4 raqami
    res2 = guestflow.scan(db, token101, "new-phone", None, last4="2222")
    db.commit()
    assert res2.kind == "mismatch"
    # Soxta/eskirgan token
    assert guestflow.scan(db, token101[:-2] + "xx", "x", None).kind == "bad_code"


def test_10_empty_room_door_without_housekeeper(db, staff):
    """10. Bo'sh xona eshigiga farrosh belgisisiz borish → R2 va video."""
    ev = door_event(db, "202")
    a = alerts_of(db, "R2")
    assert len(a) == 1 and a[0].severity == "high" and a[0].video_link == ev.video_link
    # Farrosh «ishni boshladim» bosgan bo'lsa — normal
    from app.services import housekeeping

    housekeeping.start_cleaning(db, staff["housekeeper"], room(db, "203").id)
    db.commit()
    ev2 = door_event(db, "203")
    assert ev2.verdict == "cleaning"
    assert len(alerts_of(db, "R2")) == 1


def test_11_cash_short(db, staff):
    """11. Smena kassasida 50 000 so'm kam → R9."""
    rec = staff["reception"]
    b = new_booking(db, rec)  # smena 0 so'm bilan ochiladi
    accept_and_pay_cash(db, b, rec)
    shifts.close_shift(db, rec, b.final_amount - 50_000)
    db.commit()
    a = alerts_of(db, "R9")
    assert len(a) == 1 and "-50 000" in a[0].message


def test_12_nvr_offline_events_not_lost(db, staff):
    """12. NVR tarmoqdan uzilishi → R12; ulanganda hodisalar yo'qolmaydi."""
    rec = staff["reception"]
    heartbeat(db)
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec)
    clock.advance(minutes=16)
    entry.check_edge(db)
    db.commit()
    assert alerts_of(db, "R12")
    # Oyna yopildi, lekin edge aloqasiz — R1 berilmaydi (hodisalarni kutamiz)
    clock.advance(minutes=20)
    entry.sweep_windows(db)
    db.commit()
    assert not alerts_of(db, "R1")
    # Edge qayta ulanadi va NVR'da saqlangan hodisani (oyna ichidagi vaqt bilan) yuboradi
    at = b.stage1_at + timedelta(minutes=5)
    entry.ingest_event(db, channel=1, region_id=1, event_type="regionEntrance", occurred_at=at, edge_event_id="nvr-77")
    heartbeat(db)
    entry.sweep_windows(db)
    db.commit()
    db.refresh(b)
    assert b.stage == 2
    assert not alerts_of(db, "R1")
    # Takroriy yuborish — e'tiborga olinmaydi
    assert entry.ingest_event(db, channel=1, region_id=1, event_type="regionEntrance", occurred_at=at, edge_event_id="nvr-77") is None
