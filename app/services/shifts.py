"""Smena va ko'r hisob (FT-15, TT 6.4)."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.enums import PaymentMethod, PaymentStatus, Role
from app.models import Booking, CashMovement, Payment, Shift, User
from app.services import alerts, audit, notify
from app.services.pricing import fmt_sum


class ShiftError(Exception):
    pass


def current_shift(db: Session, user: User) -> Shift | None:
    return db.scalars(
        select(Shift).where(Shift.user_id == user.id, Shift.closed_at.is_(None)).order_by(Shift.id.desc())
    ).first()


def any_open_shift(db: Session) -> Shift | None:
    return db.scalars(select(Shift).where(Shift.closed_at.is_(None)).order_by(Shift.id.desc())).first()


def open_shift(db: Session, user: User, opening_cash: int) -> Shift:
    if opening_cash < 0:
        raise ShiftError("Summa manfiy bo'lishi mumkin emas")
    if current_shift(db, user):
        raise ShiftError("Sizda ochiq smena bor")
    prev = db.scalars(select(Shift).where(Shift.closed_at.is_not(None)).order_by(Shift.closed_at.desc())).first()
    shift = Shift(user_id=user.id, opening_cash=opening_cash, opened_at=clock.now())
    if prev is not None and prev.counted_cash is not None:
        shift.handover_diff = opening_cash - prev.counted_cash
    db.add(shift)
    db.flush()
    audit.log(db, audit.actor_of(user), "shift.open", shift, new={"opening_cash": opening_cash})
    if shift.handover_diff:
        alerts.raise_alert(
            db,
            "R9",
            f"Smena qabulida farq: oldingi smena {fmt_sum(prev.counted_cash)} topshirgan, "
            f"{user.full_name} {fmt_sum(opening_cash)} qabul qildi (farq {fmt_sum(shift.handover_diff)}).",
            user=user,
            shift_id=shift.id,
            dedupe_key=f"R9:handover:{shift.id}",
        )
    return shift


def expected_cash(db: Session, shift: Shift) -> int:
    cash_in = db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.shift_id == shift.id,
            Payment.method == PaymentMethod.CASH,
            Payment.status == PaymentStatus.PAID,
        )
    )
    taken = db.scalar(
        select(func.coalesce(func.sum(CashMovement.amount), 0)).where(CashMovement.shift_id == shift.id)
    )
    return shift.opening_cash + int(cash_in or 0) - int(taken or 0)


def withdrawals(db: Session, shift: Shift) -> list[CashMovement]:
    return list(db.scalars(select(CashMovement).where(CashMovement.shift_id == shift.id).order_by(CashMovement.id)))


def withdraw(db: Session, actor: User, shift: Shift, amount: int, note: str | None = None) -> CashMovement:
    """Inkassatsiya. Faqat ega yoki administrator; reception va egaga xabar boradi."""
    if actor.role not in (Role.OWNER, Role.ADMIN):
        raise ShiftError("Kassadan pulni faqat ega yoki administrator oladi")
    if shift.closed_at is not None:
        raise ShiftError("Smena yopilgan")
    if amount <= 0:
        raise ShiftError("Summa musbat bo'lishi kerak")
    m = CashMovement(shift_id=shift.id, amount=amount, user_id=actor.id, note=(note or "").strip()[:200] or None,
                     at=clock.now())
    db.add(m)
    db.flush()
    audit.log(db, audit.actor_of(actor), "shift.withdraw", m, new={"shift": shift.id, "amount": amount, "note": m.note})
    text = f"💰 Inkassatsiya: {actor.full_name} kassadan {fmt_sum(amount)} oldi (smena: {shift.user.full_name})."
    if shift.user.telegram_id:
        notify.later(db, shift.user.telegram_id, text)
    for o in notify.owners(db):
        if o.id != actor.id:
            notify.later(db, o.telegram_id, text)
    return m


def close_shift(db: Session, user: User, counted_cash: int) -> Shift:
    """Ko'r hisob: xodim kutilgan summani ko'rmaydi, faqat sanaganini kiritadi."""
    shift = current_shift(db, user)
    if shift is None:
        raise ShiftError("Ochiq smena yo'q")
    if counted_cash < 0:
        raise ShiftError("Summa manfiy bo'lishi mumkin emas")
    shift.expected_cash = expected_cash(db, shift)
    shift.counted_cash = counted_cash
    shift.diff = counted_cash - shift.expected_cash
    shift.closed_at = clock.now()
    audit.log(
        db,
        audit.actor_of(user),
        "shift.close",
        shift,
        new={"expected": shift.expected_cash, "counted": counted_cash, "diff": shift.diff},
    )
    if shift.diff != 0:
        alerts.raise_alert(
            db,
            "R9",
            f"Smena kassasida farq: kutilgan {fmt_sum(shift.expected_cash)}, sanalgan {fmt_sum(counted_cash)}, "
            f"farq {fmt_sum(shift.diff)}.",
            user=user,
            shift_id=shift.id,
            dedupe_key=f"R9:close:{shift.id}",
        )
    from app.services import reports

    text = reports.shift_report_text(db, shift)
    for owner in notify.owners(db):
        notify.later(db, owner.telegram_id, text)
    return shift


def shift_bookings(db: Session, shift: Shift) -> list[Booking]:
    return list(db.scalars(select(Booking).where(Booking.shift_id == shift.id).order_by(Booking.id)))
