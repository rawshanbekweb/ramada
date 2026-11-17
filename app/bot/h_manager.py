"""Ega va administrator: tasdiqlar (chegirma, bekor qilish, keyin to'lash), ogohlantirishlar, hisobotlar."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.common import btn, deny, ikb, role_in
from app.enums import AlertStatus, Role
from app.i18n import t
from app.models import Alert, CancelRequest, Discount, PayLaterRequest
from app.services import alerts, bookings, discounts, notify, reports

router = Router(name="manager")


class Collect(StatesGroup):
    amount = State()
MANAGERS = (Role.OWNER, Role.ADMIN)


@router.callback_query(F.data.startswith("dc:"))
async def discount_decide(cb: CallbackQuery, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(cb, lang)
    _, verdict, did = cb.data.split(":")
    d = db.get(Discount, int(did))
    if d is None:
        return await cb.answer("?")
    try:
        changed = discounts.decide(db, d, user, approve=verdict == "ok")
    except discounts.DiscountError as e:
        return await cb.answer(str(e), show_alert=True)
    if not changed:
        return await cb.answer(t("ap.expired", lang) if d.status == "expired" else d.status, show_alert=True)
    result = t("ap.approved" if verdict == "ok" else "ap.rejected", lang)
    await cb.message.edit_text(t("ap.decided", lang, text=cb.message.html_text, result=result, who=user.full_name))
    await cb.answer(result)


@router.callback_query(F.data.startswith("cr:"))
async def cancel_decide(cb: CallbackQuery, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(cb, lang)
    _, verdict, rid = cb.data.split(":")
    req = db.get(CancelRequest, int(rid))
    if req is None or not bookings.decide_cancel(db, req, user, verdict == "ok"):
        return await cb.answer("—", show_alert=True)
    result = t("ap.approved" if verdict == "ok" else "ap.rejected", lang)
    await cb.message.edit_text(t("ap.decided", lang, text=cb.message.html_text, result=result, who=user.full_name))
    await cb.answer(result)


@router.callback_query(F.data.startswith("pl:"))
async def pay_later_decide(cb: CallbackQuery, db, user, lang):
    if not role_in(user, Role.OWNER):
        return await deny(cb, lang)
    _, verdict, rid = cb.data.split(":")
    req = db.get(PayLaterRequest, int(rid))
    if req is None or not bookings.decide_pay_later(db, req, user, verdict == "ok"):
        return await cb.answer("—", show_alert=True)
    result = t("ap.approved" if verdict == "ok" else "ap.rejected", lang)
    await cb.message.edit_text(t("ap.decided", lang, text=cb.message.html_text, result=result, who=user.full_name))
    await cb.answer(result)


@router.callback_query(F.data.startswith("al:"))
async def alert_action(cb: CallbackQuery, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(cb, lang)
    _, action, aid = cb.data.split(":")
    a = db.get(Alert, int(aid))
    if a is None:
        return await cb.answer("?")
    status = {"ack": AlertStatus.ACK, "res": AlertStatus.RESOLVED, "fp": AlertStatus.FALSE_POSITIVE}[action]
    alerts.set_status(db, a, status, user)
    await cb.answer("✅")
    await cb.message.edit_text(cb.message.html_text + f"\n\n➡️ {status} ({user.full_name})")


@router.message(btn("m.today"))
async def today(message: Message, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(message, lang)
    await message.answer(reports.today_snapshot_text(db))


@router.message(btn("m.alerts"))
async def open_alerts(message: Message, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(message, lang)
    rows = db.scalars(select(Alert).where(Alert.status == AlertStatus.NEW).order_by(Alert.at.desc()).limit(10)).all()
    if not rows:
        return await message.answer(t("al.none", lang))
    for a in reversed(rows):
        await message.answer(notify.alert_text(a, lang), reply_markup=ikb(
            [[(b["text"], b["callback_data"]) for b in notify.alert_buttons(a, lang)[-1]]]
        ))


@router.message(btn("m.report"))
async def report(message: Message, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(message, lang)
    await message.answer(reports.daily_text(db), reply_markup=ikb([[("📈 Hafta", "rep:w"), ("🗓 Oy", "rep:m"), ("📊 Bugun", "rep:t")]]))


@router.callback_query(F.data.startswith("rep:"))
async def report_more(cb: CallbackQuery, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(cb, lang)
    kind = cb.data.split(":")[1]
    if kind == "w":
        text = reports.weekly_text(db, raise_r11=False)
    elif kind == "m":
        text = reports.monthly_text(db)
    else:
        from app import clock

        text = reports.daily_text(db, clock.today())
    await cb.message.answer(text)
    await cb.answer()


@router.message(btn("m.requests"))
async def pending_requests(message: Message, db, user, lang):
    if not role_in(user, *MANAGERS):
        return await deny(message, lang)
    sent = 0
    for d in db.scalars(select(Discount).where(Discount.status == "pending")):
        if discounts.can_decide(d, user):
            await message.answer(discounts.discount_text(d, lang), reply_markup=ikb(
                [[(t("ap.approve", lang), f"dc:ok:{d.id}"), (t("ap.reject", lang), f"dc:no:{d.id}")]]))
            sent += 1
    for r in db.scalars(select(CancelRequest).where(CancelRequest.status == "pending")):
        b = r.booking_id
        await message.answer(f"🗑 #{r.id}: bron #{b} — {r.reason}", reply_markup=ikb(
            [[(t("ap.approve", lang), f"cr:ok:{r.id}"), (t("ap.reject", lang), f"cr:no:{r.id}")]]))
        sent += 1
    if user.role == Role.OWNER:
        for r in db.scalars(select(PayLaterRequest).where(PayLaterRequest.status == "pending")):
            await message.answer(f"🕓 #{r.id}: bron #{r.booking_id} — {r.reason}", reply_markup=ikb(
                [[(t("ap.approve", lang), f"pl:ok:{r.id}"), (t("ap.reject", lang), f"pl:no:{r.id}")]]))
            sent += 1
    if not sent:
        await message.answer("—")



# ------------------------------------------------------------------ inkassatsiya
@router.message(btn("m.collect"))
async def collect_start(message: Message, db, user, lang):
    from app.models import Shift
    from app.services.pricing import fmt_sum
    from app.services.shifts import expected_cash

    if not role_in(user, *MANAGERS):
        return await deny(message, lang)
    open_shifts = db.scalars(select(Shift).where(Shift.closed_at.is_(None))).all()
    if not open_shifts:
        return await message.answer(t("cash.none", lang))
    rows = [[(f"{s.user.full_name} · {fmt_sum(expected_cash(db, s))}", f"cc:{s.id}")] for s in open_shifts]
    await message.answer(t("cash.choose", lang), reply_markup=ikb(rows))


@router.callback_query(F.data.startswith("cc:"))
async def collect_pick(cb: CallbackQuery, user, lang, state: FSMContext):
    if not role_in(user, *MANAGERS):
        return await deny(cb, lang)
    await state.set_state(Collect.amount)
    await state.update_data(shift_id=int(cb.data[3:]))
    await cb.message.answer(t("cash.amount", lang))
    await cb.answer()


@router.message(Collect.amount)
async def collect_amount(message: Message, db, user, lang, state: FSMContext):
    from app.bot.common import main_menu, parse_int
    from app.models import Shift
    from app.services import shifts
    from app.services.pricing import fmt_sum

    amount = parse_int(message.text)
    data = await state.get_data()
    await state.clear()
    try:
        shifts.withdraw(db, user, db.get(Shift, data["shift_id"]), amount or 0)
    except shifts.ShiftError as e:
        db.rollback()
        return await message.answer(t("bot.error", lang, err=str(e)), reply_markup=main_menu(user))
    await message.answer(t("cash.done", lang, amount=fmt_sum(amount)), reply_markup=main_menu(user))
