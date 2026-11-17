"""Reception (va administrator) interfeysi: yangi mehmon, mehmonlar ro'yxati, to'lov, chiqish, smena."""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import select

from app import clock
from app.bot.common import btn, chunk, deny, ikb, main_menu, parse_int, role_in
from app.config import get_settings
from app.enums import BookingStatus, Role
from app.i18n import t
from app.models import Booking, KeyBoardCheck, Room
from app.services import bookings, notify, pricing, shifts
from app.services import settings_svc as cfg
from app.services.pricing import fmt_sum

router = Router(name="reception")
DESK = (Role.RECEPTION, Role.ADMIN, Role.OWNER)


class NewGuest(StatesGroup):
    room = State()
    nights = State()
    name = State()
    document = State()
    photo = State()
    phone = State()
    smartphone = State()
    here = State()
    discount_q = State()
    discount_amount = State()
    discount_reason = State()
    confirm = State()


class Pay(StatesGroup):
    amount = State()
    receipt = State()


class ShiftSt(StatesGroup):
    opening = State()
    closing = State()


class Ask(StatesGroup):
    cancel_reason = State()
    pay_later_reason = State()
    extend_nights = State()


def cancel_kb(lang):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t("bot.cancel", lang))]], resize_keyboard=True)


def stage_icon(b: Booking) -> str:
    return {0: "⏳", 1: "🟡", 2: "🟢", 3: "✅"}.get(b.stage, "")


# ------------------------------------------------------------------ new guest
@router.message(btn("m.new_guest"))
async def ng_start(message: Message, db, user, lang, state: FSMContext):
    if not role_in(user, *DESK):
        return await deny(message, lang)
    if user.role == Role.RECEPTION and shifts.current_shift(db, user) is None:
        return await message.answer(t("ng.need_shift", lang))
    today = clock.today()
    rooms = bookings.available_rooms(db, today, today + timedelta(days=1))
    if not rooms:
        return await message.answer(t("ng.no_rooms", lang))
    await state.clear()
    await state.set_state(NewGuest.room)
    rows = chunk([(f"{r.number} {r.category.code}", f"ng:room:{r.id}") for r in rooms], 4)
    await message.answer(t("ng.choose_room", lang), reply_markup=cancel_kb(lang))
    await message.answer("🏨", reply_markup=ikb(rows))


@router.callback_query(NewGuest.room, F.data.startswith("ng:room:"))
async def ng_room(cb: CallbackQuery, lang, state: FSMContext):
    await state.update_data(room_id=int(cb.data.split(":")[2]))
    await state.set_state(NewGuest.nights)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(t("ng.nights", lang), reply_markup=ikb([[(str(i), f"ng:n:{i}") for i in range(1, 8)]]))
    await cb.answer()


async def _after_nights(message: Message, lang, state: FSMContext, n: int):
    if n < 1 or n > 60:
        return await message.answer(t("r.amount_bad", lang))
    await state.update_data(nights=n)
    await state.set_state(NewGuest.name)
    await message.answer(t("ng.name", lang))


@router.callback_query(NewGuest.nights, F.data.startswith("ng:n:"))
async def ng_nights_cb(cb: CallbackQuery, lang, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await _after_nights(cb.message, lang, state, int(cb.data.split(":")[2]))
    await cb.answer()


@router.message(NewGuest.nights)
async def ng_nights_text(message: Message, lang, state: FSMContext):
    await _after_nights(message, lang, state, parse_int(message.text) or 0)


@router.message(NewGuest.name, F.text)
async def ng_name(message: Message, lang, state: FSMContext):
    await state.update_data(name=message.text.strip()[:150])
    await state.set_state(NewGuest.document)
    await message.answer(t("ng.document", lang))


@router.message(NewGuest.document, F.text)
async def ng_document(message: Message, lang, state: FSMContext):
    await state.update_data(document=message.text.strip()[:40])
    await state.set_state(NewGuest.photo)
    await message.answer(t("ng.photo", lang))


@router.message(NewGuest.photo, F.photo)
async def ng_photo(message: Message, bot: Bot, lang, state: FSMContext):
    photo = message.photo[-1]
    folder = Path(get_settings().storage_dir) / "passports" / clock.today().strftime("%Y%m")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.jpg"
    try:
        await bot.download(photo, destination=path)
        saved = str(path)
    except Exception:  # pragma: no cover - tarmoq
        saved = None
    await state.update_data(photo_tg=photo.file_id, photo_path=saved)
    await state.set_state(NewGuest.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱", request_contact=True)], [KeyboardButton(text=t("bot.cancel", lang))]],
        resize_keyboard=True,
    )
    await message.answer(t("ng.phone", lang), reply_markup=kb)


@router.message(NewGuest.photo)
async def ng_photo_need(message: Message, lang):
    await message.answer(t("ng.photo_need", lang))


@router.message(NewGuest.phone)
async def ng_phone(message: Message, lang, state: FSMContext):
    raw = message.contact.phone_number if message.contact else (message.text or "")
    try:
        phone = bookings.normalize_phone(raw)
    except bookings.BookingError:
        return await message.answer(t("ng.phone_bad", lang))
    await state.update_data(phone=phone)
    await state.set_state(NewGuest.smartphone)
    await message.answer(t("ng.smartphone", lang), reply_markup=cancel_kb(lang))
    await message.answer("📱", reply_markup=ikb([[(t("yes", lang), "ng:sp:1"), (t("no", lang), "ng:sp:0")]]))


@router.callback_query(NewGuest.smartphone, F.data.startswith("ng:sp:"))
async def ng_smartphone(cb: CallbackQuery, lang, state: FSMContext):
    await state.update_data(smartphoneless=cb.data.endswith(":0"))
    await state.set_state(NewGuest.here)
    await cb.message.edit_text(t("ng.here", lang), reply_markup=ikb([[(t("yes", lang), "ng:here:1"), (t("no", lang), "ng:here:0")]]))
    await cb.answer()


@router.callback_query(NewGuest.here, F.data.startswith("ng:here:"))
async def ng_here(cb: CallbackQuery, lang, state: FSMContext):
    await state.update_data(arrived=cb.data.endswith(":1"))
    await state.set_state(NewGuest.discount_q)
    await cb.message.edit_text(t("ng.discount_q", lang), reply_markup=ikb([[(t("no", lang), "ng:dq:0"), (t("yes", lang), "ng:dq:1")]]))
    await cb.answer()


@router.callback_query(NewGuest.discount_q, F.data.startswith("ng:dq:"))
async def ng_discount_q(cb: CallbackQuery, db, lang, state: FSMContext):
    await cb.answer()
    if cb.data.endswith(":1"):
        await state.set_state(NewGuest.discount_amount)
        await cb.message.edit_text(t("ng.discount_amount", lang))
        return
    await state.update_data(discount_amount=0, discount_reason=None)
    await cb.message.delete()
    await _summary(cb.message, db, lang, state)


@router.message(NewGuest.discount_amount)
async def ng_discount_amount(message: Message, lang, state: FSMContext):
    amount = parse_int(message.text)
    if not amount:
        return await message.answer(t("r.amount_bad", lang))
    await state.update_data(discount_amount=amount)
    await state.set_state(NewGuest.discount_reason)
    await message.answer(t("ng.discount_reason", lang))


@router.message(NewGuest.discount_reason, F.text)
async def ng_discount_reason(message: Message, db, lang, state: FSMContext):
    await state.update_data(discount_reason=message.text.strip()[:500])
    await _summary(message, db, lang, state)


async def _summary(message: Message, db, lang, state: FSMContext):
    data = await state.get_data()
    room = db.get(Room, data["room_id"])
    checkin = clock.today()
    try:
        quote = pricing.quote(db, room.category_id, checkin, data["nights"], arrival=clock.now() if data.get("arrived") else None)
    except pricing.PricingError as e:
        await state.clear()
        return await message.answer(t("bot.error", lang, err=str(e)))
    extra = ""
    if data.get("discount_amount"):
        extra = f"Chegirma so'rovi: {fmt_sum(data['discount_amount'])} — {data.get('discount_reason')}\n"
    if data.get("smartphoneless"):
        extra += "📵 smartfonsiz\n"
    await state.set_state(NewGuest.confirm)
    await message.answer(
        t(
            "ng.summary",
            lang,
            room=room.number,
            category=room.category.name,
            dates=f"{checkin:%d.%m}–{checkin + timedelta(days=data['nights']):%d.%m}",
            nights=data["nights"],
            name=data["name"],
            phone=data["phone"],
            official=fmt_sum(quote.total),
            extra=extra,
        ),
        reply_markup=ikb([[(t("confirm", lang), "ng:ok"), (t("bot.cancel", lang), "ng:no")]]),
    )


@router.callback_query(NewGuest.confirm, F.data.in_({"ng:ok", "ng:no"}))
async def ng_confirm(cb: CallbackQuery, db, user, lang, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    await state.clear()
    if cb.data == "ng:no":
        await cb.message.edit_text(t("bot.cancelled", lang))
        return await cb.message.answer(t("bot.menu", lang), reply_markup=main_menu(user))
    try:
        b = bookings.create_booking(
            db,
            user=user,
            room_id=data["room_id"],
            nights=data["nights"],
            guest_name=data["name"],
            phone=data["phone"],
            document=data.get("document"),
            photo_path=data.get("photo_path"),
            photo_tg=data.get("photo_tg"),
            smartphoneless=data.get("smartphoneless", False),
            arrived=data.get("arrived", True),
            discount_amount=data.get("discount_amount") or 0,
            discount_reason=data.get("discount_reason"),
        )
    except Exception as e:  # BookingError, DiscountError, PricingError
        db.rollback()
        await cb.message.edit_text(t("bot.error", lang, err=str(e)))
        return await cb.message.answer(t("bot.menu", lang), reply_markup=main_menu(user))
    key = "ng.created_nophone" if b.smartphoneless else ("ng.created_discount" if b.discount_status == "pending" else "ng.created")
    await cb.message.edit_text(t(key, lang, id=b.id))
    await cb.message.answer(booking_card(b, lang), reply_markup=booking_kb(b, user, lang))
    await cb.message.answer(t("bot.menu", lang), reply_markup=main_menu(user))


# ------------------------------------------------------------------ guests list / card
def booking_card(b: Booking, lang: str) -> str:
    lines = [
        f"{stage_icon(b)} <b>#{b.id} · {b.room.number}</b> ({b.room.category.name}) · {b.stage}/3 · {t('status.' + b.status, lang)}",
        f"👤 {b.guest.full_name} · {b.guest.phone}{' · 📵' if b.smartphoneless else ''}",
        f"📅 {b.checkin_date:%d.%m} → {b.checkout_date:%d.%m} ({b.nights})",
        f"💰 {fmt_sum(b.final_amount)} · to'langan {fmt_sum(b.paid_amount)}"
        + (f" · qoldiq {fmt_sum(b.balance_due)}" if b.balance_due > 0 else ""),
    ]
    if b.status == BookingStatus.PENDING and not b.smartphoneless:
        lines.append(("✅ " if b.guest_accepted_at else "⏳ ") + t("r.wait_guest", lang).lstrip("⏳ "))
    if b.discount_status == "pending":
        lines.append("💸 chegirma kutilmoqda")
    return "\n".join(lines)


def booking_kb(b: Booking, user, lang: str):
    rows = []
    if b.is_open:
        rows.append([(t("b.pay_cash", lang), f"ba:cash:{b.id}"), (t("b.pay_terminal", lang), f"ba:term:{b.id}")])
    if b.status == BookingStatus.PENDING:
        rows.append([(t("b.key", lang), f"ba:key:{b.id}"), (t("b.resend", lang), f"ba:sms:{b.id}")])
        rows.append([(t("b.pay_later", lang), f"ba:pl:{b.id}"), (t("b.cancel", lang), f"ba:cancel:{b.id}")])
        if b.arrival_at is None:
            rows.append([(t("b.arrived", lang), f"ba:arr:{b.id}")])
    elif b.status == BookingStatus.ACTIVE:
        rows.append([(t("b.key", lang), f"ba:key:{b.id}"), (t("b.checkout", lang), f"ba:out:{b.id}")])
        rows.append([(t("b.extend", lang), f"ba:ext:{b.id}"), (t("b.change_room", lang), f"ba:chg:{b.id}")])
        row = [(t("b.cancel", lang), f"ba:cancel:{b.id}")]
        if b.arrival_at is None:
            row.append((t("b.arrived", lang), f"ba:arr:{b.id}"))
        rows.append(row)
    rows.append([(t("b.photo", lang), f"ba:photo:{b.id}"), ("🔄", f"b:{b.id}")])
    return ikb(rows)


@router.message(btn("m.guests"))
async def guests_list(message: Message, db, user, lang):
    if not role_in(user, *DESK):
        return await deny(message, lang)
    rows = bookings.open_bookings(db)
    if not rows:
        return await message.answer(t("r.no_guests", lang))
    buttons = [(f"{stage_icon(b)} {b.room.number} · {b.guest.full_name.split()[0]}", f"b:{b.id}") for b in rows[:60]]
    await message.answer(t("m.guests", lang), reply_markup=ikb(chunk(buttons, 2)))


@router.callback_query(F.data.regexp(r"^b:\d+$"))
async def guest_card(cb: CallbackQuery, db, user, lang):
    if not role_in(user, *DESK):
        return await deny(cb, lang)
    b = db.get(Booking, int(cb.data[2:]))
    await cb.message.answer(booking_card(b, lang), reply_markup=booking_kb(b, user, lang))
    await cb.answer()


@router.callback_query(F.data.startswith("ba:"))
async def booking_action(cb: CallbackQuery, db, user, lang, state: FSMContext, bot: Bot):
    if not role_in(user, *DESK):
        return await deny(cb, lang)
    _, action, bid = cb.data.split(":")
    b = db.get(Booking, int(bid))
    if b is None:
        return await cb.answer("?")
    await cb.answer()
    try:
        if action in ("cash", "term"):
            await state.set_state(Pay.amount)
            await state.update_data(booking_id=b.id, method="cash" if action == "cash" else "terminal")
            due = bookings.amount_due_now(db, b) or b.balance_due
            await cb.message.answer(t("r.pay_amount", lang, due=fmt_sum(due)), reply_markup=cancel_kb(lang))
        elif action == "key":
            ok = bookings.issue_key(db, b, user)
            await cb.message.answer(t("r.key_ok" if ok else "r.key_refused", lang, id=b.id))
        elif action == "sms":
            bookings.send_guest_link(db, b)
            await cb.message.answer(t("r.sms_resent", lang))
        elif action == "arr":
            opened = bookings.mark_arrived(db, b, user)
            if opened:
                await cb.message.answer(t("r.arrived_ok", lang, minutes=cfg.get_int(db, "entry_window_minutes")))
            else:
                await cb.message.answer("✅")
        elif action == "out":
            res = bookings.checkout(db, b, user)
            if res["ok"]:
                await cb.message.answer(t("r.checkout_done", lang, id=b.id, extra=""))
            else:
                await cb.message.answer(
                    t("r.checkout_due", lang, due=fmt_sum(res["due"]), late=fmt_sum(res["late"])),
                    reply_markup=booking_kb(b, user, lang),
                )
        elif action == "ext":
            await state.set_state(Ask.extend_nights)
            await state.update_data(booking_id=b.id)
            await cb.message.answer(t("r.extend_nights", lang), reply_markup=cancel_kb(lang))
        elif action == "chg":
            start = max(clock.today(), b.checkin_date)
            rooms = bookings.available_rooms(db, start, b.checkout_date)
            rows = chunk([(f"{r.number} {r.category.code}", f"chg:{b.id}:{r.id}") for r in rooms], 4)
            await cb.message.answer(t("r.change_room", lang), reply_markup=ikb(rows))
        elif action == "cancel":
            await state.set_state(Ask.cancel_reason)
            await state.update_data(booking_id=b.id)
            await cb.message.answer(t("r.cancel_reason", lang), reply_markup=cancel_kb(lang))
        elif action == "pl":
            await state.set_state(Ask.pay_later_reason)
            await state.update_data(booking_id=b.id)
            await cb.message.answer(t("r.pay_later_reason", lang), reply_markup=cancel_kb(lang))
        elif action == "photo":
            if b.guest.document_photo_tg:
                await bot.send_photo(cb.from_user.id, b.guest.document_photo_tg, caption=f"#{b.id} {b.guest.full_name}")
    except bookings.BookingError as e:
        db.rollback()
        await cb.message.answer(t("bot.error", lang, err=str(e)))


@router.callback_query(F.data.startswith("chg:"))
async def change_room_pick(cb: CallbackQuery, db, user, lang):
    if not role_in(user, *DESK):
        return await deny(cb, lang)
    _, bid, rid = cb.data.split(":")
    b = db.get(Booking, int(bid))
    try:
        am = bookings.change_room(db, b, user, int(rid))
        await cb.message.edit_text(t("r.amend_sent", lang, amount=fmt_sum(am.extra_amount)))
    except bookings.BookingError as e:
        db.rollback()
        await cb.message.answer(t("bot.error", lang, err=str(e)))
    await cb.answer()


@router.message(Ask.extend_nights)
async def extend_nights(message: Message, db, user, lang, state: FSMContext):
    n = parse_int(message.text)
    data = await state.get_data()
    await state.clear()
    b = db.get(Booking, data["booking_id"])
    try:
        am = bookings.extend(db, b, user, n or 0)
        await message.answer(t("r.amend_sent", lang, amount=fmt_sum(am.extra_amount)), reply_markup=main_menu(user))
    except bookings.BookingError as e:
        db.rollback()
        await message.answer(t("bot.error", lang, err=str(e)), reply_markup=main_menu(user))


@router.message(Ask.cancel_reason, F.text)
async def cancel_reason(message: Message, db, user, lang, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    b = db.get(Booking, data["booking_id"])
    try:
        if user.role in (Role.OWNER, Role.ADMIN):
            bookings.cancel(db, b, user, message.text)
            await message.answer(t("bot.cancelled", lang), reply_markup=main_menu(user))
        else:
            bookings.request_cancel(db, b, user, message.text)
            await message.answer(t("r.cancel_requested", lang), reply_markup=main_menu(user))
    except bookings.BookingError as e:
        db.rollback()
        await message.answer(t("bot.error", lang, err=str(e)), reply_markup=main_menu(user))


@router.message(Ask.pay_later_reason, F.text)
async def pay_later_reason(message: Message, db, user, lang, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    b = db.get(Booking, data["booking_id"])
    bookings.request_pay_later(db, b, user, message.text.strip()[:500])
    await message.answer(t("r.cancel_requested", lang).replace("🗑", "🕓"), reply_markup=main_menu(user))


# ------------------------------------------------------------------ payment (cash/terminal)
@router.message(Pay.amount)
async def pay_amount(message: Message, lang, state: FSMContext):
    amount = parse_int(message.text)
    if not amount:
        return await message.answer(t("r.amount_bad", lang))
    await state.update_data(amount=amount)
    await state.set_state(Pay.receipt)
    await message.answer(t("r.pay_receipt", lang))


@router.message(Pay.receipt, F.text)
async def pay_receipt(message: Message, db, user, lang, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    b = db.get(Booking, data["booking_id"])
    try:
        bookings.record_payment(db, b, data["method"], data["amount"], user=user, receipt_no=message.text.strip()[:60])
    except bookings.BookingError as e:
        db.rollback()
        return await message.answer(t("bot.error", lang, err=str(e)), reply_markup=main_menu(user))
    await message.answer(t("r.pay_saved", lang, amount=fmt_sum(data["amount"]), due=fmt_sum(max(0, b.balance_due))),
                         reply_markup=main_menu(user))
    if b.status == BookingStatus.PENDING and not b.smartphoneless and b.guest_accepted_at is None:
        await message.answer(t("r.wait_guest", lang))
    await message.answer(booking_card(b, lang), reply_markup=booking_kb(b, user, lang))


# ------------------------------------------------------------------ shift
@router.message(btn("m.shift"))
async def shift_menu(message: Message, db, user, lang, state: FSMContext):
    if not role_in(user, Role.RECEPTION, Role.ADMIN):
        return await deny(message, lang)
    sh = shifts.current_shift(db, user)
    if sh is None:
        await state.set_state(ShiftSt.opening)
        return await message.answer(t("sh.open_q", lang), reply_markup=cancel_kb(lang))
    await message.answer(
        t("sh.status", lang, opened=clock.fmt(sh.opened_at)),
        reply_markup=ikb([[(t("sh.close_btn", lang), "sh:close")]]),
    )


@router.message(ShiftSt.opening)
async def shift_open(message: Message, db, user, lang, state: FSMContext):
    amount = parse_int(message.text)
    if amount is None:
        return await message.answer(t("r.amount_bad", lang))
    await state.clear()
    try:
        shifts.open_shift(db, user, amount)
    except shifts.ShiftError as e:
        return await message.answer(t("bot.error", lang, err=str(e)), reply_markup=main_menu(user))
    await message.answer(t("sh.opened", lang), reply_markup=main_menu(user))


@router.callback_query(F.data == "sh:close")
async def shift_close_ask(cb: CallbackQuery, lang, state: FSMContext):
    await state.set_state(ShiftSt.closing)
    await cb.message.answer(t("sh.close_q", lang), reply_markup=cancel_kb(lang))
    await cb.answer()


@router.message(ShiftSt.closing)
async def shift_close(message: Message, db, user, lang, state: FSMContext):
    amount = parse_int(message.text)
    if amount is None:
        return await message.answer(t("r.amount_bad", lang))
    await state.clear()
    try:
        shifts.close_shift(db, user, amount)
    except shifts.ShiftError as e:
        return await message.answer(t("bot.error", lang, err=str(e)), reply_markup=main_menu(user))
    # Ko'r hisob: xodimga kutilgan summa ko'rsatilmaydi
    await message.answer(t("sh.closed", lang), reply_markup=main_menu(user))


# ------------------------------------------------------------------ rooms overview
@router.message(btn("m.rooms"))
async def rooms_overview(message: Message, db, user, lang):
    if not role_in(user, *DESK):
        return await deny(message, lang)
    rooms = db.scalars(select(Room).where(Room.active.is_(True)).order_by(Room.floor, Room.number)).all()
    active = {b.room_id: b for b in bookings.open_bookings(db)}
    icon = {"clean": "⬜", "dirty": "🟫", "cleaning": "🧽", "out_of_order": "⛔"}
    lines, floor = [], None
    for r in rooms:
        if r.floor != floor:
            floor = r.floor
            lines.append(f"\n<b>{floor}</b>")
        b = active.get(r.id)
        lines.append(f"{stage_icon(b) if b else icon.get(r.state, '')} {r.number}" + (f" · {b.stage}/3" if b else ""))
    await message.answer("\n".join(lines))


# ------------------------------------------------------------------ key board photo (FT-46)
@router.message(F.photo)
async def keyboard_photo(message: Message, db, user, lang):
    if user is None:
        return
    chk = db.scalars(
        select(KeyBoardCheck)
        .where(KeyBoardCheck.user_id == user.id, KeyBoardCheck.answered_at.is_(None))
        .order_by(KeyBoardCheck.id.desc())
    ).first()
    if chk is None:
        return
    chk.photo_tg = message.photo[-1].file_id
    chk.answered_at = clock.now()
    delay = int((chk.answered_at - chk.requested_at).total_seconds() // 60)
    caption = (
        f"📸 Kalit taxtasi: {user.full_name}, {delay} daqiqada javob berdi.\n"
        f"Band xonalar (kalit tashqarida bo'lishi kerak): {', '.join(chk.expected_out) or '—'}"
    )
    from app.integrations import telegram as tg

    def _send(_s2, file_id=chk.photo_tg, text=caption):
        for o in notify.owners(_s2):
            tg.backend().send_photo(o.telegram_id, file_id, text)

    notify.later_call(db, _send)
    await message.answer("✅")
