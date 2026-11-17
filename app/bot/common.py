"""Bot uchun umumiy yordamchilar: foydalanuvchini yuklash, menyular, filtrlash."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import Role
from app.i18n import STAFF_LANGS, t
from app.models import User

log = logging.getLogger(__name__)


class DbUserMiddleware(BaseMiddleware):
    """Har bir yangilanish uchun DB sessiyasi va xodimni (telegram_id bo'yicha) beradi."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        db = SessionLocal()
        try:
            user = None
            if tg_user is not None:
                user = db.scalar(select(User).where(User.telegram_id == tg_user.id, User.active.is_(True)))
            data["db"] = db
            data["user"] = user
            data["lang"] = (user.lang if user else None) or "uz"
            result = await handler(event, data)
            db.commit()
            return result
        except Exception:
            db.rollback()
            log.exception("Bot handler xatosi")
            raise
        finally:
            db.close()


def texts(key: str) -> set[str]:
    return {t(key, lang) for lang in STAFF_LANGS}


def btn(key: str):
    """Reply-tugma matni bo'yicha filtr (barcha xodim tillarida)."""
    return F.text.in_(texts(key))


MENUS = {
    Role.RECEPTION: [["m.new_guest", "m.guests"], ["m.shift", "m.rooms"], ["m.lang"]],
    Role.ADMIN: [["m.new_guest", "m.guests"], ["m.shift", "m.requests"], ["m.today", "m.alerts"], ["m.rooms", "m.collect"], ["m.lang"]],
    Role.OWNER: [["m.today", "m.alerts"], ["m.report", "m.requests"], ["m.guests", "m.new_guest"], ["m.collect", "m.lang"]],
    Role.HOUSEKEEPER: [["m.morning", "m.cleaning"], ["m.qr_plates", "m.linen"], ["m.lang"]],
}


def main_menu(user: User) -> ReplyKeyboardMarkup:
    rows = MENUS.get(user.role, [["m.lang"]])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(k, user.lang)) for k in row] for row in rows],
        resize_keyboard=True,
    )


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


def chunk(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def role_in(user: User | None, *roles: str) -> bool:
    return user is not None and user.role in roles


async def deny(event: Message | CallbackQuery, lang: str) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer(t("bot.no_access", lang), show_alert=True)
    else:
        await event.answer(t("bot.no_access", lang))


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits or len(digits) > 12:
        return None
    return int(digits)


MENU_KEYS = sorted({k for rows in MENUS.values() for row in rows for k in row})


class MenuResetMiddleware(BaseMiddleware):
    """Jarayon o'rtasida asosiy menyu tugmasi bosilsa — eski holat (FSM) tozalanadi.

    Aks holda, masalan, «Necha tun?» savolidan keyin «📋 Mehmonlar» bosilsa, keyingi
    raqam eski jarayonga yozilib ketardi.
    """

    async def __call__(self, handler, event, data):
        state = data.get("state")
        text = getattr(event, "text", None)
        if state is not None and text and text in _menu_texts():
            if await state.get_state() is not None:
                await state.clear()
        return await handler(event, data)


_MENU_TEXTS: set[str] | None = None


def _menu_texts() -> set[str]:
    global _MENU_TEXTS
    if _MENU_TEXTS is None:
        _MENU_TEXTS = set().union(*(texts(k) for k in MENU_KEYS))
    return _MENU_TEXTS
