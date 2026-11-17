"""Bron va uch bosqichli tasdiq zanjiri (FT-01..FT-07, FT-13, FT-14)."""
from __future__ import annotations

import re
from datetime import date, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app import clock
from app.enums import (
    AmendmentKind,
    AmendmentStatus,
    BookingStatus,
    CancelRequestStatus,
    ConfirmSource,
    PaymentMethod,
    PaymentStatus,
    Role,
    RoomState,
)
from app.i18n import t
from app.models import (
    Amendment,
    Booking,
    CancelRequest,
    Confirmation,
    Guest,
    Occupancy,
    PayLaterRequest,
    Payment,
    Room,
    User,
)
from app.security import new_guest_token
from app.services import alerts, audit, notify, pricing, shifts
from app.services import settings_svc as cfg
from app.services.pricing import fmt_sum


class BookingError(Exception):
    pass


PHONE_RE = re.compile(r"^\+?998\d{9}$")


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 9:
        digits = "998" + digits
    if not PHONE_RE.match(digits):
        raise BookingError("Telefon raqami noto'g'ri")
    return "+" + digits


# ------------------------------------------------------------------ rooms
def overlapping(db: Session, room_id: int, checkin: date, checkout: date, exclude_id: int | None = None):
    q = select(Booking).where(
        Booking.room_id == room_id,
        Booking.status.in_([BookingStatus.PENDING, BookingStatus.ACTIVE]),
        Booking.checkin_date < checkout,
        Booking.checkout_date > checkin,
    )
    if exclude_id:
        q = q.where(Booking.id != exclude_id)
    return db.scalars(q).first()


def available_rooms(db: Session, checkin: date, checkout: date, require_clean: bool = True) -> list[Room]:
    rooms = db.scalars(select(Room).where(Room.active.is_(True)).order_by(Room.floor, Room.number)).all()
    out = []
    for r in rooms:
        if r.state == RoomState.OUT_OF_ORDER:
            continue
        if require_clean and r.state != RoomState.CLEAN:
            continue
        if overlapping(db, r.id, checkin, checkout) is None:
            out.append(r)
    return out


def current_booking(db: Session, room_id: int) -> Booking | None:
    """Xonadagi hozirgi (to'langan, yashayotgan) bron."""
    return db.scalars(
        select(Booking)
        .where(Booking.room_id == room_id, Booking.status == BookingStatus.ACTIVE)
        .order_by(Booking.id.desc())
    ).first()


def open_bookings(db: Session) -> list[Booking]:
    return list(
        db.scalars(
            select(Booking)
            .where(Booking.status.in_([BookingStatus.PENDING, BookingStatus.ACTIVE]))
            .order_by(Booking.id.desc())
        )
    )


# ------------------------------------------------------------------ create
def create_booking(
    db: Session,
    *,
    user: User,
    room_id: int,
    nights: int,
    guest_name: str,
    phone: str,
    document: str | None = None,
    photo_path: str | None = None,
    photo_tg: str | None = None,
    smartphoneless: bool = False,
    arrived: bool = True,
    checkin_date: date | None = None,
    discount_amount: int = 0,
    discount_reason: str | None = None,
    lang: str | None = None,
) -> Booking:
    if not (photo_path or photo_tg):
        raise BookingError("Pasport surati majburiy (FT-03)")
    phone = normalize_phone(phone)
    if not guest_name or not guest_name.strip():
        raise BookingError("Mehmon ismi kiritilmagan")
    if nights < 1 or nights > 60:
        raise BookingError("Tunlar soni 1..60")
    shift = shifts.current_shift(db, user)
    if user.role == Role.RECEPTION and shift is None:
        raise BookingError("Avval smenani oching")
    room = db.get(Room, room_id)
    if room is None or not room.active or room.state == RoomState.OUT_OF_ORDER:
        raise BookingError("Xona mavjud emas yoki ishlamaydi")
    now = clock.now()
    checkin = checkin_date or clock.today()
    # Tungi soatlarda (00:00 – erta kirish chegarasi) kelgan mehmon kechagi tun uchun yoziladi
    if checkin_date is None and arrived:
        local_now = clock.local(now)
        if local_now.hour < cfg.get_int(db, "early_checkin_half_from_hour"):
            checkin = local_now.date() - timedelta(days=1)
    checkout = checkin + timedelta(days=nights)
    clash = overlapping(db, room.id, checkin, checkout)
    if clash is not None:
        raise BookingError(f"Xona {room.number} bu sanalarda band (bron #{clash.id})")

    quote = pricing.quote(db, room.category_id, checkin, nights, arrival=now if arrived else None)
    guest = Guest(
        full_name=guest_name.strip(),
        document=(document or "").strip() or None,
        phone=phone,
        document_photo=photo_path,
        document_photo_tg=photo_tg,
        lang=lang,
    )
    db.add(guest)
    db.flush()
    booking = Booking(
        room_id=room.id,
        guest_id=guest.id,
        checkin_date=checkin,
        checkout_date=checkout,
        arrival_at=now if arrived else None,
        official_amount=quote.total,
        final_amount=quote.total,
        price_breakdown=quote.lines,
        smartphoneless=smartphoneless,
        guest_token=new_guest_token(),
        guest_token_expires_at=now + timedelta(days=cfg.get_int(db, "guest_link_days")),
        created_by_id=user.id,
        shift_id=shift.id if shift else None,
        created_at=now,
        status=BookingStatus.PENDING,
    )
    db.add(booking)
    db.flush()
    audit.log(
        db,
        audit.actor_of(user),
        "booking.create",
        booking,
        new={
            "room": room.number,
            "nights": nights,
            "official": quote.total,
            "phone": phone,
            "smartphoneless": smartphoneless,
        },
    )
    if discount_amount and discount_amount > 0:
        from app.services import discounts

        discounts.request_discount(db, booking, user, discount_amount, discount_reason or "")
    elif not smartphoneless:
        send_guest_link(db, booking)
    return booking


def recompute_final(booking: Booking) -> None:
    booking.final_amount = max(0, booking.official_amount - booking.discount_amount + booking.extra_amount)


def guest_lang(booking: Booking) -> str:
    return booking.guest.lang or "uz"


def send_guest_link(db: Session, booking: Booking) -> bool:
    if booking.smartphoneless:
        return False
    lang = guest_lang(booking)
    text = t(
        "sms.link",
        lang,
        hotel=cfg.get(db, "hotel_name"),
        room=booking.room.number,
        category=booking.room.category.name,
        dates=f"{booking.checkin_date:%d.%m}–{booking.checkout_date:%d.%m}",
        nights=booking.nights,
        url=notify.guest_url(booking.guest_token),
    )
    ok = notify.send_sms(db, booking.guest.phone, text, "link", booking.id)
    booking.sms_sent_at = clock.now()
    return ok


# ------------------------------------------------------------------ stage 1
def guest_accept(db: Session, booking: Booking, device_hash: str | None) -> None:
    """Mehmon o'z telefonida xona, muddat va rasmiy narxni tasdiqladi."""
    if booking.status != BookingStatus.PENDING:
        return
    if booking.discount_status == "pending":
        raise BookingError("Chegirma hali ko'rib chiqilmoqda")
    booking.guest_accepted_at = clock.now()
    booking.guest_accepted_amount = booking.final_amount
    if device_hash and not booking.guest_device_hash:
        booking.guest_device_hash = device_hash
    audit.log(db, "guest", "booking.guest_accept", booking, new={"amount": booking.final_amount})
    evaluate_stage1(db, booking)


def stage1_ready(booking: Booking) -> bool:
    if booking.status != BookingStatus.PENDING:
        return False
    if booking.discount_status == "pending":
        return False
    accepted = booking.smartphoneless or (
        booking.guest_accepted_at is not None and booking.guest_accepted_amount == booking.final_amount
    )
    paid = booking.paid_amount >= booking.final_amount or booking.pay_later_approved
    return accepted and paid


def evaluate_stage1(db: Session, booking: Booking) -> bool:
    if not stage1_ready(booking):
        return False
    now = clock.now()
    source = ConfirmSource.PAYMENT if booking.paid_amount >= booking.final_amount else ConfirmSource.PAY_LATER
    booking.status = BookingStatus.ACTIVE
    booking.stage = 1
    booking.stage1_at = now
    db.add(
        Confirmation(
            booking_id=booking.id,
            stage=1,
            source=source,
            at=now,
            evidence={
                "payments": [p.id for p in booking.payments if p.status == PaymentStatus.PAID],
                "paid": booking.paid_amount,
                "accepted_at": booking.guest_accepted_at.isoformat() if booking.guest_accepted_at else None,
                "smartphoneless": booking.smartphoneless,
            },
        )
    )
    db.add(Occupancy(booking_id=booking.id, room_id=booking.room_id, start_at=now))
    audit.log(db, "system", "booking.stage1", booking, new={"source": source})
    from app.services import entry

    if booking.arrival_at is not None:
        entry.open_window(db, booking)
        notify.tell_user(
            db,
            booking.created_by,
            "r.give_key",
            id=booking.id,
            room=booking.room.number,
            minutes=cfg.get_int(db, "entry_window_minutes"),
        )
    else:
        notify.tell_user(db, booking.created_by, "r.give_key_late", id=booking.id)
    # Mehmon xonaga QR'ni oldinroq skanerlagan bo'lishi mumkin
    refresh_stage(db, booking)
    return True


def refresh_stage(db: Session, booking: Booking) -> None:
    """Ketma-ket o'tilgan bosqichlar sonini qayta hisoblash."""
    if booking.stage1_at is None:
        return
    stages = {c.stage for c in booking.confirmations if not c.void}
    new_stage = 1
    if 2 in stages:
        new_stage = 2
        if 3 in stages:
            new_stage = 3
    if new_stage > booking.stage:
        now = clock.now()
        if new_stage >= 2 and booking.stage2_at is None:
            booking.stage2_at = now
        if new_stage >= 3 and booking.stage3_at is None:
            booking.stage3_at = now
        booking.stage = new_stage
        audit.log(db, "system", "booking.stage", booking, new={"stage": new_stage})


def add_confirmation(db: Session, booking: Booking, stage: int, source: str, evidence: dict) -> Confirmation:
    c = Confirmation(booking_id=booking.id, stage=stage, source=source, at=clock.now(), evidence=evidence)
    db.add(c)
    booking.confirmations.append(c)
    db.flush()
    refresh_stage(db, booking)
    return c


# ------------------------------------------------------------------ payments
def record_payment(
    db: Session,
    booking: Booking,
    method: str,
    amount: int,
    *,
    user: User | None = None,
    receipt_no: str | None = None,
    external_id: str | None = None,
    actor: str | None = None,
) -> Payment:
    if amount <= 0:
        raise BookingError("Summa musbat bo'lishi kerak")
    if not booking.is_open:
        raise BookingError("Bron yopilgan")
    shift = None
    if method in (PaymentMethod.CASH, PaymentMethod.TERMINAL):
        if not receipt_no or not receipt_no.strip():
            raise BookingError("Fiskal chek raqami majburiy (FT-14)")
        if user is None:
            raise BookingError("Naqd/terminal to'lovni xodim kiritadi")
        shift = shifts.current_shift(db, user)
        if shift is None:
            raise BookingError("Naqd/terminal to'lov uchun smena ochiq bo'lishi kerak")
    else:
        open_shift = shifts.any_open_shift(db)
        shift = open_shift
    pending_amendment = get_pending_amendment(db, booking)
    p = Payment(
        booking_id=booking.id,
        method=method,
        amount=amount,
        status=PaymentStatus.PAID,
        external_id=external_id,
        receipt_no=receipt_no.strip() if receipt_no else None,
        shift_id=shift.id if shift else None,
        created_by_id=user.id if user else None,
        amendment_id=pending_amendment.id if pending_amendment and booking.paid_amount >= booking.final_amount else None,
        created_at=clock.now(),
        paid_at=clock.now(),
    )
    db.add(p)
    booking.payments.append(p)
    db.flush()
    audit.log(
        db,
        actor or audit.actor_of(user, method),
        "payment.record",
        p,
        new={"booking": booking.id, "method": method, "amount": amount, "receipt": receipt_no},
    )
    lang = guest_lang(booking)
    notify.send_sms(
        db,
        booking.guest.phone,
        t(
            "sms.receipt",
            lang,
            hotel=cfg.get(db, "hotel_name"),
            amount=fmt_sum(amount),
            room=booking.room.number,
            url=notify.guest_url(booking.guest_token),
        ),
        "receipt",
        booking.id,
    )
    if booking.status == BookingStatus.PENDING:
        evaluate_stage1(db, booking)
    else:
        try_apply_amendment(db, booking)
    return p


def amount_due_now(db: Session, booking: Booking) -> int:
    """Mehmon sahifasida hozir to'lanadigan summa (asosiy qoldiq + tasdiqlangan o'zgarish)."""
    due = booking.final_amount - booking.paid_amount
    am = get_pending_amendment(db, booking)
    if am is not None and (am.guest_accepted_at or booking.smartphoneless):
        due += am.extra_amount
    return max(0, due)


# ------------------------------------------------------------------ key / arrival
def issue_key(db: Session, booking: Booking, user: User) -> bool:
    """Kalit berish. 1/3 dan o'tmagan bo'lsa — rad va egaga xabar (16-bo'lim, 1-sinov)."""
    if booking.stage < 1:
        alerts.raise_alert(
            db,
            "KEY",
            f"{user.full_name} bron #{booking.id} ({booking.room.number}) uchun mehmon tasdig'i va "
            f"to'lovsiz kalit berishga urindi. To'langan: {fmt_sum(booking.paid_amount)} / "
            f"{fmt_sum(booking.final_amount)}.",
            booking=booking,
            user=user,
            dedupe_key=f"KEY:{booking.id}:{clock.now():%Y%m%d%H%M}",
        )
        audit.log(db, audit.actor_of(user), "booking.key_refused", booking)
        return False
    booking.key_issued_at = clock.now()
    audit.log(db, audit.actor_of(user), "booking.key_issued", booking)
    return True


def mark_arrived(db: Session, booking: Booking, user: User) -> bool:
    """Oldindan to'langan bron: kirish oynasi «mehmon keldi» bosilganda ochiladi (6.6)."""
    if not booking.is_open:
        raise BookingError("Bron yopilgan")
    from app.services import entry

    if booking.arrival_at is None:
        booking.arrival_at = clock.now()
    audit.log(db, audit.actor_of(user), "booking.arrived", booking)
    if booking.stage >= 1 and entry.active_window(db, booking) is None and booking.stage < 2:
        entry.open_window(db, booking)
        return True
    return False


# ------------------------------------------------------------------ checkout
def checkout(db: Session, booking: Booking, user: User, force: bool = False) -> dict:
    """Chiqish. Kech chiqish haqi avtomatik qo'shiladi (FT-12)."""
    if booking.status != BookingStatus.ACTIVE:
        raise BookingError("Faqat yashayotgan mehmon chiqariladi")
    now = clock.now()
    late = None
    if not any(line.get("kind") == "late_checkout" for line in booking.price_breakdown):
        late = pricing.late_checkout_line(db, booking.room.category_id, booking.checkout_date, now)
        if late is not None:
            booking.price_breakdown = list(booking.price_breakdown) + [late]
            booking.extra_amount += late["amount"]
            recompute_final(booking)
            audit.log(db, audit.actor_of(user), "booking.late_fee", booking, new=late)
    due = booking.balance_due
    if due > 0 and not (force and user.role in (Role.OWNER, Role.ADMIN)):
        return {"ok": False, "due": due, "late": late["amount"] if late else 0}
    booking.status = BookingStatus.CHECKED_OUT
    booking.checked_out_at = now
    _close_occupancy(db, booking, now)
    from app.services import entry

    entry.supersede_windows(db, booking)
    booking.room.state = RoomState.DIRTY
    audit.log(db, audit.actor_of(user), "booking.checkout", booking, new={"due": due, "forced": bool(due)})
    return {"ok": True, "due": due, "late": late["amount"] if late else 0}


def _close_occupancy(db: Session, booking: Booking, at) -> None:
    for occ in db.scalars(
        select(Occupancy).where(Occupancy.booking_id == booking.id, Occupancy.end_at.is_(None))
    ):
        occ.end_at = at


# ------------------------------------------------------------------ cancel
def cancel(db: Session, booking: Booking, user: User, reason: str) -> None:
    """FT-05: o'chirish taqiqlangan, faqat sababli bekor qilish va egaga xabar."""
    if not booking.is_open:
        raise BookingError("Bron allaqachon yopilgan")
    if not reason or not reason.strip():
        raise BookingError("Sabab majburiy")
    if user.role not in (Role.OWNER, Role.ADMIN):
        raise BookingError("Reception faqat so'rov orqali bekor qiladi")
    was = booking.status
    paid = booking.paid_amount
    now = clock.now()
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now
    booking.cancel_reason = reason.strip()
    _close_occupancy(db, booking, now)
    from app.services import entry

    entry.supersede_windows(db, booking)
    for am in db.scalars(
        select(Amendment).where(Amendment.booking_id == booking.id, Amendment.status == AmendmentStatus.PENDING)
    ):
        am.status = AmendmentStatus.CANCELLED
    if was == BookingStatus.ACTIVE and booking.room.state == RoomState.CLEAN:
        booking.room.state = RoomState.DIRTY
    audit.log(db, audit.actor_of(user), "booking.cancel", booking, old={"status": was}, new={"reason": reason})
    if paid > 0 or booking.stage >= 1:
        alerts.raise_alert(
            db,
            "R10",
            f"Bron #{booking.id} ({booking.room.number}) to'lovdan keyin bekor qilindi. "
            f"To'langan: {fmt_sum(paid)}. Sabab: {reason}. Bekor qildi: {user.full_name}.",
            booking=booking,
            dedupe_key=f"R10:{booking.id}",
        )
    else:
        for owner in notify.owners(db):
            notify.later(
                db,
                owner.telegram_id,
                f"ℹ️ Bron #{booking.id} ({booking.room.number}) bekor qilindi (to'lovsiz). "
                f"Sabab: {reason}. Kim: {user.full_name}",
            )


def request_cancel(db: Session, booking: Booking, user: User, reason: str) -> CancelRequest:
    if not booking.is_open:
        raise BookingError("Bron allaqachon yopilgan")
    req = CancelRequest(booking_id=booking.id, reason=reason, requested_by_id=user.id, requested_at=clock.now())
    db.add(req)
    db.flush()
    audit.log(db, audit.actor_of(user), "booking.cancel_request", req, new={"booking": booking.id, "reason": reason})
    for u in notify.users_with_roles(db, [Role.OWNER, Role.ADMIN]):
        notify.later(
            db,
            u.telegram_id,
            t(
                "ap.cancel",
                u.lang,
                id=req.id,
                booking=booking.id,
                room=booking.room.number,
                status=t(f"status.{booking.status}", u.lang) + f" {booking.stage}/3",
                paid=fmt_sum(booking.paid_amount),
                reason=reason,
                who=user.full_name,
            ),
            [
                [
                    {"text": t("ap.approve", u.lang), "callback_data": f"cr:ok:{req.id}"},
                    {"text": t("ap.reject", u.lang), "callback_data": f"cr:no:{req.id}"},
                ]
            ],
        )
    return req


def decide_cancel(db: Session, req: CancelRequest, user: User, approve: bool) -> bool:
    if req.status != CancelRequestStatus.PENDING:
        return False
    if user.role not in (Role.OWNER, Role.ADMIN):
        raise BookingError("Huquq yo'q")
    req.status = CancelRequestStatus.APPROVED if approve else CancelRequestStatus.REJECTED
    req.decided_by_id = user.id
    req.decided_at = clock.now()
    booking = db.get(Booking, req.booking_id)
    if approve and booking.is_open:
        cancel(db, booking, user, req.reason)
    requester = db.get(User, req.requested_by_id)
    if requester and requester.telegram_id:
        notify.later(
            db,
            requester.telegram_id,
            f"Bron #{booking.id}: bekor qilish — {t('ap.approved' if approve else 'ap.rejected', requester.lang)}",
        )
    return True


# ------------------------------------------------------------------ pay later
def request_pay_later(db: Session, booking: Booking, user: User, reason: str) -> PayLaterRequest:
    req = PayLaterRequest(booking_id=booking.id, reason=reason, requested_by_id=user.id, requested_at=clock.now())
    db.add(req)
    db.flush()
    audit.log(db, audit.actor_of(user), "booking.pay_later_request", req, new={"booking": booking.id})
    for u in notify.owners(db):
        notify.later(
            db,
            u.telegram_id,
            t(
                "ap.pay_later",
                u.lang,
                id=req.id,
                booking=booking.id,
                room=booking.room.number,
                amount=fmt_sum(booking.final_amount),
                reason=reason,
                who=user.full_name,
            ),
            [
                [
                    {"text": t("ap.approve", u.lang), "callback_data": f"pl:ok:{req.id}"},
                    {"text": t("ap.reject", u.lang), "callback_data": f"pl:no:{req.id}"},
                ]
            ],
        )
    return req


def decide_pay_later(db: Session, req: PayLaterRequest, user: User, approve: bool) -> bool:
    """FT-04: «keyin to'lash» faqat ega tasdig'i bilan."""
    if req.status != CancelRequestStatus.PENDING:
        return False
    if user.role != Role.OWNER:
        raise BookingError("Faqat ega tasdiqlaydi")
    req.status = CancelRequestStatus.APPROVED if approve else CancelRequestStatus.REJECTED
    req.decided_by_id = user.id
    req.decided_at = clock.now()
    booking = db.get(Booking, req.booking_id)
    audit.log(db, audit.actor_of(user), "booking.pay_later_decide", req, new={"approve": approve})
    if approve:
        booking.pay_later_approved = True
        evaluate_stage1(db, booking)
    requester = db.get(User, req.requested_by_id)
    if requester and requester.telegram_id:
        notify.later(
            db,
            requester.telegram_id,
            f"Bron #{booking.id}: keyin to'lash — {t('ap.approved' if approve else 'ap.rejected', requester.lang)}",
        )
    return True


# ------------------------------------------------------------------ amendments (FT-06)
def get_pending_amendment(db: Session, booking: Booking) -> Amendment | None:
    return db.scalars(
        select(Amendment)
        .where(Amendment.booking_id == booking.id, Amendment.status == AmendmentStatus.PENDING)
        .order_by(Amendment.id.desc())
    ).first()


def _new_amendment(db: Session, booking: Booking, user: User, **kw) -> Amendment:
    if booking.status != BookingStatus.ACTIVE:
        raise BookingError("Faqat yashayotgan mehmon broni o'zgartiriladi")
    if get_pending_amendment(db, booking) is not None:
        raise BookingError("Oldingi o'zgarish hali tasdiqlanmagan")
    am = Amendment(booking_id=booking.id, created_by_id=user.id, created_at=clock.now(), **kw)
    db.add(am)
    db.flush()
    audit.log(db, audit.actor_of(user), f"amendment.{am.kind}", am, new={"extra": am.extra_amount})
    if booking.smartphoneless:
        if am.extra_amount == 0:
            apply_amendment(db, booking, am)
    else:
        lang = guest_lang(booking)
        what = (
            t("a.extend", lang, date=f"{am.new_checkout_date:%d.%m}")
            if am.kind == AmendmentKind.EXTEND
            else t("a.room_change", lang, room=am.new_room.number)
        )
        notify.send_sms(
            db,
            booking.guest.phone,
            t("sms.amendment", lang, hotel=cfg.get(db, "hotel_name"), what=what, url=notify.guest_url(booking.guest_token)),
            "amendment",
            booking.id,
        )
        # Havola muddati uzaytiriladi
        booking.guest_token_expires_at = max(
            booking.guest_token_expires_at, clock.now() + timedelta(days=cfg.get_int(db, "guest_link_days"))
        )
    return am


def extend(db: Session, booking: Booking, user: User, extra_nights: int) -> Amendment:
    if extra_nights < 1:
        raise BookingError("Kamida 1 tun")
    new_checkout = booking.checkout_date + timedelta(days=extra_nights)
    clash = overlapping(db, booking.room_id, booking.checkout_date, new_checkout, exclude_id=booking.id)
    if clash is not None:
        raise BookingError(f"Xona bu sanalarda band (bron #{clash.id})")
    lines = pricing.stay_lines(db, booking.room.category_id, booking.checkout_date, extra_nights)
    return _new_amendment(
        db,
        booking,
        user,
        kind=AmendmentKind.EXTEND,
        new_checkout_date=new_checkout,
        extra_amount=sum(x["amount"] for x in lines),
        breakdown=lines,
    )


def change_room(db: Session, booking: Booking, user: User, new_room_id: int) -> Amendment:
    new_room = db.get(Room, new_room_id)
    if new_room is None or new_room.id == booking.room_id or not new_room.active:
        raise BookingError("Yangi xona noto'g'ri")
    start = max(clock.today(), booking.checkin_date)
    if start >= booking.checkout_date:
        raise BookingError("Bron muddati tugagan")
    clash = overlapping(db, new_room.id, start, booking.checkout_date)
    if clash is not None:
        raise BookingError(f"Xona {new_room.number} band (bron #{clash.id})")
    nights = (booking.checkout_date - start).days
    old_lines = pricing.stay_lines(db, booking.room.category_id, start, nights)
    new_lines = pricing.stay_lines(db, new_room.category_id, start, nights)
    diff = sum(x["amount"] for x in new_lines) - sum(x["amount"] for x in old_lines)
    breakdown = [
        {"kind": "room_change", "from": booking.room.number, "to": new_room.number, "nights": nights, "amount": max(0, diff)}
    ]
    return _new_amendment(
        db,
        booking,
        user,
        kind=AmendmentKind.ROOM_CHANGE,
        new_room_id=new_room.id,
        extra_amount=max(0, diff),
        breakdown=breakdown,
    )


def guest_accept_amendment(db: Session, booking: Booking, am: Amendment) -> None:
    if am.status != AmendmentStatus.PENDING:
        return
    am.guest_accepted_at = clock.now()
    audit.log(db, "guest", "amendment.guest_accept", am)
    try_apply_amendment(db, booking)


def try_apply_amendment(db: Session, booking: Booking) -> bool:
    am = get_pending_amendment(db, booking)
    if am is None:
        return False
    if not booking.smartphoneless and am.guest_accepted_at is None:
        return False
    if booking.paid_amount < booking.final_amount + am.extra_amount:
        return False
    apply_amendment(db, booking, am)
    return True


def apply_amendment(db: Session, booking: Booking, am: Amendment) -> None:
    now = clock.now()
    booking.extra_amount += am.extra_amount
    booking.price_breakdown = list(booking.price_breakdown) + list(am.breakdown)
    recompute_final(booking)
    am.status = AmendmentStatus.APPLIED
    am.applied_at = now
    if am.kind == AmendmentKind.EXTEND:
        booking.checkout_date = am.new_checkout_date
    else:
        from app.services import entry

        old_room = booking.room
        _close_occupancy(db, booking, now)
        entry.supersede_windows(db, booking)
        booking.room_id = am.new_room_id
        booking.room = am.new_room
        old_room.state = RoomState.DIRTY
        db.add(Occupancy(booking_id=booking.id, room_id=am.new_room_id, start_at=now))
        # Yangi xona uchun kirish va xonadagi tasdiq qaytadan talab qilinadi
        db.flush()
        booking.stage = 1
        booking.stage2_at = booking.stage3_at = booking.qr_confirmed_at = None
        for c in list(booking.confirmations):
            if c.stage >= 2:
                c.evidence = dict(c.evidence or {}, superseded_by_room_change=am.id)
                c.void = True  # tarix uchun saqlanadi, hisobga olinmaydi
        entry.open_window(db, booking)
        notify.tell_user(
            db,
            booking.created_by,
            "r.give_key",
            id=booking.id,
            room=booking.room.number,
            minutes=cfg.get_int(db, "entry_window_minutes"),
        )
    audit.log(db, "system", "amendment.apply", am, new={"kind": am.kind})


# ------------------------------------------------------------------ queries
def find_active_by_device(db: Session, device_hash: str | None) -> Booking | None:
    if not device_hash:
        return None
    return db.scalars(
        select(Booking)
        .where(
            Booking.guest_device_hash == device_hash,
            Booking.status.in_([BookingStatus.ACTIVE, BookingStatus.PENDING]),
        )
        .order_by(Booking.id.desc())
    ).first()


def bookings_between(db: Session, start, end) -> list[Booking]:
    return list(
        db.scalars(
            select(Booking).where(and_(Booking.created_at >= start, Booking.created_at < end)).order_by(Booking.id)
        )
    )
