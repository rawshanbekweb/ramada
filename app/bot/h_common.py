from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.common import btn, ikb, main_menu
from app.i18n import LANG_NAMES, STAFF_LANGS, t
from app.models import User
from app.services import audit

router = Router(name="common")


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, db, user, lang, state: FSMContext):
    await state.clear()
    if user is not None:
        await message.answer(t("bot.welcome", lang, name=user.full_name, role=t(f"role.{user.role}", lang)),
                             reply_markup=main_menu(user))
        return
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer(t("bot.unknown_user", lang))
        return
    u = db.scalar(select(User).where(User.invite_code == code, User.active.is_(True)))
    if u is None or u.telegram_id is not None:
        await message.answer(t("bot.bad_invite", lang))
        return
    u.telegram_id = message.from_user.id
    u.invite_code = None
    audit.log(db, f"user:{u.id}", "user.telegram_linked", u, new={"tg": message.from_user.id})
    await message.answer(t("bot.welcome", u.lang, name=u.full_name, role=t(f"role.{u.role}", u.lang)),
                         reply_markup=main_menu(u))


@router.message(Command("menu"))
async def menu(message: Message, user, lang, state: FSMContext):
    await state.clear()
    if user is None:
        await message.answer(t("bot.unknown_user", lang))
        return
    await message.answer(t("bot.menu", lang), reply_markup=main_menu(user))


@router.message(Command("cancel"))
@router.message(F.text.in_({t("bot.cancel", x) for x in STAFF_LANGS}))
async def cancel(message: Message, user, lang, state: FSMContext):
    await state.clear()
    if user:
        await message.answer(t("bot.cancelled", lang), reply_markup=main_menu(user))


@router.message(Command("lang"))
@router.message(btn("m.lang"))
async def lang_choose(message: Message, user, lang):
    if user is None:
        return
    await message.answer(t("bot.lang_choose", lang), reply_markup=ikb([[(LANG_NAMES[x], f"lang:{x}") for x in STAFF_LANGS]]))


@router.callback_query(F.data.startswith("lang:"))
async def lang_set(cb: CallbackQuery, db, user):
    if user is None:
        return
    new = cb.data.split(":")[1]
    if new in STAFF_LANGS:
        user.lang = new
    await cb.message.answer(t("bot.lang_set", user.lang), reply_markup=main_menu(user))
    await cb.answer()
