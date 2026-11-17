from datetime import date, datetime, timedelta

from sqlalchemy import select

from app import clock
from app.enums import BookingStatus
from app.models import Alert, Discount
from app.services import bookings, discounts, housekeeping, pricing, rules
from tests.helpers import accept_and_pay_cash, door_event, new_booking, room


def test_discount_admin_then_timeout(db, staff, telegram, sms):
    rec = staff["reception"]
    b = new_booking(db, rec, discount_amount=50_000, discount_reason="doimiy mijoz")
    assert b.discount_status == "pending"
    assert not sms.sent  # SMS chegirma qaroridan keyin
    d = db.scalar(select(Discount))
    assert d.approver_role == "admin"  # 5% < 10%
    assert any("dc:ok" in str(m["buttons"]) and m["chat_id"] == 1002 for m in telegram.sent)
    discounts.decide(db, d, staff["admin"], approve=True)
    db.commit()
    assert b.final_amount == b.official_amount - 50_000
    assert sms.sent and "/g/" in sms.sent[-1][1]
    # Katta chegirma — faqat ega; 10 daqiqada javob yo'q — rad
    b2 = new_booking(db, rec, number="102", discount_amount=400_000, discount_reason="tanish")
    d2 = db.scalar(select(Discount).where(Discount.booking_id == b2.id))
    assert d2.approver_role == "owner"
    try:
        discounts.decide(db, d2, staff["admin"], approve=True)
        assert False, "admin katta chegirmani tasdiqlay olmaydi"
    except discounts.DiscountError:
        db.rollback()
    clock.advance(minutes=11)
    discounts.expire_overdue(db)
    db.commit()
    db.refresh(b2)
    assert b2.discount_status == "expired" and b2.final_amount == b2.official_amount


def test_pricing_weekend_holiday_early_late(db, staff):
    cat = room(db, "101").category_id
    lines = pricing.stay_lines(db, cat, date(2026, 12, 18), 3)  # Ju, Sha, Ya
    assert [x["day_type"] for x in lines] == ["weekend", "weekend", "weekday"]
    early = pricing.early_checkin_line(db, cat, date(2026, 12, 15), clock.local_dt(date(2026, 12, 15), 9))
    assert early["percent"] == 50
    late = pricing.late_checkout_line(db, cat, date(2026, 12, 17), clock.local_dt(date(2026, 12, 17), 20))
    assert late["percent"] == 100


def test_checkout_with_late_fee(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec, nights=1)
    accept_and_pay_cash(db, b, rec)
    clock.set_now(datetime(2026, 12, 16, 15, 0))  # 12:00 dan keyin
    res = bookings.checkout(db, b, rec)
    assert res["ok"] is False and res["late"] == 250_000
    bookings.record_payment(db, b, "cash", res["due"], user=rec, receipt_no="L-1")
    res = bookings.checkout(db, b, rec)
    db.commit()
    assert res["ok"] and b.status == BookingStatus.CHECKED_OUT and b.room.state == "dirty"
    # Chiqishdan keyin farroshsiz harakat — R2
    clock.advance(minutes=30)
    door_event(db, "101")
    assert db.scalar(select(Alert).where(Alert.rule == "R2"))


def test_room_change_requires_new_confirmation(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec, number="101", nights=2)
    accept_and_pay_cash(db, b, rec)
    door_event(db, "101")
    assert b.stage == 2
    am = bookings.change_room(db, b, rec, room(db, "112").id)  # Standart -> Lyuks
    db.commit()
    assert am.extra_amount > 0
    bookings.guest_accept_amendment(db, b, am)
    bookings.record_payment(db, b, "cash", am.extra_amount, user=rec, receipt_no="RC-1")
    db.commit()
    assert b.room.number == "112" and b.stage == 1
    door_event(db, "112")
    db.refresh(b)
    assert b.stage == 2


def test_cancel_after_payment_r10(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec)
    accept_and_pay_cash(db, b, rec)
    try:
        bookings.cancel(db, b, rec, "xato")
        assert False
    except bookings.BookingError:
        pass
    req = bookings.request_cancel(db, b, rec, "mehmon ketdi")
    bookings.decide_cancel(db, req, staff["admin"], True)
    db.commit()
    assert b.status == BookingStatus.CANCELLED
    assert db.scalar(select(Alert).where(Alert.rule == "R10"))


def test_pay_later_owner_only(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec)
    bookings.guest_accept(db, b, "d")
    req = bookings.request_pay_later(db, b, rec, "korporativ")
    try:
        bookings.decide_pay_later(db, req, staff["admin"], True)
        assert False
    except bookings.BookingError:
        pass
    bookings.decide_pay_later(db, req, staff["owner"], True)
    db.commit()
    assert b.stage == 1


def test_smartphoneless_housekeeper_stage3_and_r7(db, staff):
    rec, hk = staff["reception"], staff["housekeeper"]
    b = new_booking(db, rec, number="101", smartphoneless=True)
    bookings.record_payment(db, b, "terminal", b.final_amount, user=rec, receipt_no="T-9")
    db.commit()
    assert b.stage == 1
    door_event(db, "101")
    clock.set_now(datetime(2026, 12, 16, 9, 0))
    raised = housekeeping.save_morning_check(db, hk, {room(db, "101").id: True, room(db, "305").id: True})
    db.commit()
    db.refresh(b)
    assert b.stage == 3
    assert len(raised) == 1 and raised[0].rule == "R7" and raised[0].room.number == "305"


def test_qr_reminder_and_r6(db, staff, sms):
    rec = staff["reception"]
    b = new_booking(db, rec)
    accept_and_pay_cash(db, b, rec)
    clock.advance(minutes=61)
    assert rules.qr_reminders(db) == 1
    assert rules.qr_reminders(db) == 0  # bir marta
    clock.advance(hours=24)
    rules.r6_not_full(db)
    db.commit()
    assert db.scalar(select(Alert).where(Alert.rule == "R6", Alert.booking_id == b.id))


def test_extension(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec, nights=1)
    accept_and_pay_cash(db, b, rec)
    am = bookings.extend(db, b, rec, 2)
    assert b.checkout_date == date(2026, 12, 16)
    bookings.guest_accept_amendment(db, b, am)
    assert bookings.amount_due_now(db, b) == am.extra_amount
    bookings.record_payment(db, b, "cash", am.extra_amount, user=rec, receipt_no="E-1")
    db.commit()
    assert b.checkout_date == date(2026, 12, 18)
    assert b.balance_due == 0


def test_nvr_down_marks_unverifiable(db, staff):
    from app.services import entry

    rec = staff["reception"]
    entry.heartbeat(db, {"nvr_ok": False})
    b = new_booking(db, rec)
    accept_and_pay_cash(db, b, rec)
    clock.advance(minutes=31)
    entry.heartbeat(db, {"nvr_ok": False})
    entry.sweep_windows(db)
    db.commit()
    db.refresh(b)
    assert b.stage == 2
    assert any(c.source == "camera_unverifiable" for c in b.confirmations)


def test_daily_weekly_reports(db, staff):
    from app.services import reports

    rec = staff["reception"]
    b = new_booking(db, rec)
    accept_and_pay_cash(db, b, rec)
    text = reports.daily_text(db, clock.today())
    assert "Kunlik hisobot" in text
    assert "Haftalik" in reports.weekly_text(db, clock.today() - timedelta(days=clock.today().weekday()))
    assert "Oylik" in reports.monthly_text(db, clock.today().replace(day=1))
    from app.services import export

    assert export.build_xlsx(db, clock.today(), clock.today() + timedelta(days=1))[:2] == b"PK"
