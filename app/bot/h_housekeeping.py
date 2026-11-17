"""Farrosh: ertalabki tekshiruv, tozalash, haftalik QR plita, choyshab (TT 6.5)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app import clock
from app.bot.common import btn, chunk, deny, ikb, main_menu, role_in
from app.enums import Role
from app.i18n import t
from app.models import Room, RoomCheck
from app.services import housekeeping

router = Router(name="housekeeping")
HK = (Role.HOUSEKEEPER, Role.ADMIN, Role.OWNER)


class Linen(StatesGroup):
    counts = State()


def _grid(rooms, marks: dict, prefix: str, on: str, off: str, lang: str, save_cb: str):
    buttons = [(f"{on if marks.get(str(r.id)) else off} {r.number}", f"{prefix}:{r.id}") for r in rooms]
    rows = chunk(buttons, 4)
    rows.append([(t("hk.save", lang), save_cb)])
    return ikb(rows)


@router.message(btn("m.morning"))
async def morning(message: Message, db, user, lang, state: FSMContext):
    if not role_in(user, *HK):
        return await deny(message, lang)
    rooms = housekeeping.rooms(db)
    day = clock.today()
    existing = {c.room_id: c.used for c in db.scalars(select(RoomCheck).where(RoomCheck.day == day, RoomCheck.kind == "morning"))}
    # Boshlang'ich belgi: oldingi kecha band bo'lgan xonalar «ishlatilgan» emas — farrosh o'zi ko'rib belgilaydi
    marks = {str(r.id): bool(existing.get(r.id, False)) for r in rooms}
    await state.update_data(morning=marks)
    await message.answer(t("hk.morning_intro", lang), reply_markup=_grid(rooms, marks, "hm", "✅", "⬜", lang, "hm:save"))


@router.callback_query(F.data.startswith("hm:"))
async def morning_toggle(cb: CallbackQuery, db, user, lang, state: FSMContext):
    if not role_in(user, *HK):
        return await deny(cb, lang)
    data = await state.get_data()
    marks = data.get("morning") or {}
    key = cb.data.split(":")[1]
    if key == "save":
        raised = housekeeping.save_morning_check(db, user, {int(k): v for k, v in marks.items()})
        await state.update_data(morning=None)
        await cb.message.edit_text(t("hk.saved", lang) + (f" ⚠️ {len(raised)}" if raised else ""))
        return await cb.answer()
    marks[key] = not marks.get(key, False)
    await state.update_data(morning=marks)
    await cb.message.edit_reply_markup(reply_markup=_grid(housekeeping.rooms(db), marks, "hm", "✅", "⬜", lang, "hm:save"))
    await cb.answer()


@router.message(btn("m.cleaning"))
async def cleaning(message: Message, db, user, lang):
    if not role_in(user, *HK):
        return await deny(message, lang)
    open_s = housekeeping.open_cleaning(db, user)
    if open_s is not None:
        room = db.get(Room, open_s.room_id)
        return await message.answer(
            t("hk.started", lang, room=room.number),
            reply_markup=ikb([[(t("hk.finish_btn", lang, room=room.number), f"hc:done:{open_s.id}")]]),
        )
    rooms = housekeeping.rooms(db)
    rooms.sort(key=lambda r: (r.state not in ("dirty",), r.floor, r.number))
    icon = {"dirty": "🟫", "cleaning": "🧽", "clean": "⬜", "out_of_order": "⛔"}
    rows = chunk([(f"{icon.get(r.state, '')} {r.number}", f"hc:start:{r.id}") for r in rooms], 4)
    await message.answer(t("hk.start_choose", lang), reply_markup=ikb(rows))


@router.callback_query(F.data.startswith("hc:"))
async def cleaning_action(cb: CallbackQuery, db, user, lang):
    if not role_in(user, *HK):
        return await deny(cb, lang)
    _, action, ident = cb.data.split(":")
    if action == "start":
        s = housekeeping.start_cleaning(db, user, int(ident))
        room = db.get(Room, s.room_id)
        await cb.message.edit_text(
            t("hk.started", lang, room=room.number),
            reply_markup=ikb([[(t("hk.finish_btn", lang, room=room.number), f"hc:done:{s.id}")]]),
        )
    else:
        s = housekeeping.finish_cleaning(db, user, int(ident))
        if s is not None:
            await cb.message.edit_text(t("hk.finished", lang, room=db.get(Room, s.room_id).number))
    await cb.answer()


@router.message(btn("m.qr_plates"))
async def qr_plates(message: Message, db, user, lang, state: FSMContext):
    if not role_in(user, *HK):
        return await deny(message, lang)
    rooms = housekeeping.rooms(db)
    marks = {str(r.id): True for r in rooms}
    await state.update_data(qr=marks)
    await message.answer(t("hk.qr_intro", lang), reply_markup=_grid(rooms, marks, "hq", "✅", "❌", lang, "hq:save"))


@router.callback_query(F.data.startswith("hq:"))
async def qr_toggle(cb: CallbackQuery, db, user, lang, state: FSMContext):
    if not role_in(user, *HK):
        return await deny(cb, lang)
    data = await state.get_data()
    marks = data.get("qr") or {}
    key = cb.data.split(":")[1]
    if key == "save":
        housekeeping.save_qr_plate_check(db, user, {int(k): v for k, v in marks.items()})
        await state.update_data(qr=None)
        await cb.message.edit_text(t("hk.saved", lang))
        return await cb.answer()
    marks[key] = not marks.get(key, True)
    await state.update_data(qr=marks)
    await cb.message.edit_reply_markup(reply_markup=_grid(housekeeping.rooms(db), marks, "hq", "✅", "❌", lang, "hq:save"))
    await cb.answer()


@router.message(btn("m.linen"))
async def linen(message: Message, user, lang, state: FSMContext):
    if not role_in(user, *HK):
        return await deny(message, lang)
    await state.set_state(Linen.counts)
    await message.answer(t("hk.linen_q", lang))


@router.message(Linen.counts, F.text)
async def linen_counts(message: Message, db, user, lang, state: FSMContext):
    parts = [p for p in message.text.replace(",", " ").split() if p.isdigit()]
    if len(parts) != 2:
        return await message.answer(t("hk.linen_q", lang))
    await state.clear()
    housekeeping.log_linen(db, user, int(parts[0]), int(parts[1]))
    await message.answer(t("hk.saved", lang), reply_markup=main_menu(user))
