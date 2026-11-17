"""Chegirma so'rovi va tasdig'i (FT-11, TT 6.2)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.enums import DiscountStatus, Role
from app.i18n import t
from app.models import Booking, Discount, User
from app.services import audit, bookings, notify
from app.services import settings_svc as cfg
from app.services.pricing import fmt_sum


class DiscountError(Exception):
    pass


def request_discount(db: Session, booking: Booking, user: User, amount: int, reason: str) -> Discount:
    if amount <= 0 or amount >= booking.official_amount:
        raise DiscountError("Chegirma summasi noto'g'ri")
    if not reason.strip():
        raise DiscountError("Chegirma sababi majburiy")
    pct = amount * 100 / booking.official_amount
    limit = cfg.get_int(db, "discount_auto_limit_percent")
    approver_role = Role.ADMIN if pct <= limit else Role.OWNER
    timeout = cfg.get_int(db, "discount_timeout_minutes")
    d = Discount(
        booking_id=booking.id,
        amount=amount,
        reason=reason.strip(),
        requested_by_id=user.id,
        approver_role=approver_role,
        requested_at=clock.now(),
        expires_at=clock.now() + timedelta(minutes=timeout),
        status=DiscountStatus.PENDING,
    )
    db.add(d)
    booking.discount_status = DiscountStatus.PENDING
    db.flush()
    audit.log(db, audit.actor_of(user), "discount.request", d, new={"amount": amount, "pct": round(pct, 1)})
    roles = [Role.OWNER] if approver_role == Role.OWNER else [Role.ADMIN, Role.OWNER]
    approvers = notify.users_with_roles(db, roles)
    # Administrator chegarasidagi chegirma: avval administratorlar, ular bo'lmasa — ega
    if approver_role == Role.ADMIN and any(u.role == Role.ADMIN for u in approvers):
        approvers = [u for u in approvers if u.role == Role.ADMIN]
    _send_requests(db, d, booking, user, approvers, pct, timeout)
    return d


def _send_requests(db, d: Discount, booking: Booking, user: User, approvers: list[User], pct: float, timeout: int):
    def _do(s2: Session, discount_id: int, approver_ids: list[int]):
        from app.integrations import telegram as tg

        disc = s2.get(Discount, discount_id)
        msgs = []
        for uid in approver_ids:
            u = s2.get(User, uid)
            text = discount_text(disc, u.lang, pct, timeout)
            mid = tg.backend().send_message(
                u.telegram_id,
                text,
                [
                    [
                        {"text": t("ap.approve", u.lang), "callback_data": f"dc:ok:{disc.id}"},
                        {"text": t("ap.reject", u.lang), "callback_data": f"dc:no:{disc.id}"},
                    ]
                ],
            )
            if mid:
                msgs.append([u.telegram_id, mid, u.lang])
        disc.tg_messages = msgs

    notify.later_call(db, _do, d.id, [u.id for u in approvers])


def discount_text(d: Discount, lang: str, pct: float | None = None, timeout: int | None = None) -> str:
    b = d.booking
    pct = pct if pct is not None else d.amount * 100 / b.official_amount
    return t(
        "ap.discount",
        lang,
        id=d.id,
        booking=b.id,
        room=b.room.number,
        nights=b.nights,
        official=fmt_sum(b.official_amount),
        amount=fmt_sum(d.amount),
        pct=round(pct, 1),
        reason=d.reason,
        who=d.requested_by.full_name,
        minutes=timeout or 10,
    )


def can_decide(d: Discount, user: User) -> bool:
    if user.role == Role.OWNER:
        return True
    return user.role == Role.ADMIN and d.approver_role == Role.ADMIN


def decide(db: Session, d: Discount, user: User | None, approve: bool, expired: bool = False) -> bool:
    if d.status != DiscountStatus.PENDING:
        return False
    if user is not None and not can_decide(d, user):
        raise DiscountError("Bu chegirmani tasdiqlash huquqi yo'q")
    booking = d.booking
    if approve:
        d.status = DiscountStatus.APPROVED
        booking.discount_amount = d.amount
        booking.discount_status = DiscountStatus.APPROVED
    else:
        d.status = DiscountStatus.EXPIRED if expired else DiscountStatus.REJECTED
        booking.discount_status = d.status
    d.approved_by_id = user.id if user else None
    d.decided_at = clock.now()
    bookings.recompute_final(booking)
    audit.log(db, audit.actor_of(user), "discount.decide", d, new={"status": d.status})
    result_key = "ap.approved" if approve else ("ap.expired" if expired else "ap.rejected")
    # Tasdiqlovchilardagi xabarni yangilash
    who = user.full_name if user else "—"
    for chat_id, mid, lang in d.tg_messages or []:
        notify._outbox(db).append(("call", _edit, (chat_id, mid, discount_text(d, lang), t(result_key, lang), who)))
    # Mehmonga havola faqat qarordan keyin
    if booking.status == "pending":
        bookings.send_guest_link(db, booking)
    notify.tell_user(db, d.requested_by, "ap.discount_result", booking=booking.id, result=t(result_key, d.requested_by.lang))
    return True


def _edit(_s2, chat_id, mid, text, result, who):
    from app.integrations import telegram as tg

    tg.backend().edit_message(chat_id, mid, t("ap.decided", None, text=text, result=result, who=who))


def expire_overdue(db: Session) -> int:
    """10 daqiqada javob yo'q — rad (FT-11)."""
    rows = db.scalars(
        select(Discount).where(Discount.status == DiscountStatus.PENDING, Discount.expires_at <= clock.now())
    ).all()
    for d in rows:
        decide(db, d, None, approve=False, expired=True)
    return len(rows)
