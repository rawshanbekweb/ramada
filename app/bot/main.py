"""Telegram botni yig'ish."""
from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import h_common, h_housekeeping, h_manager, h_reception
from app.bot.common import DbUserMiddleware, MenuResetMiddleware
from app.config import get_settings


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DbUserMiddleware())
    dp.message.outer_middleware(MenuResetMiddleware())
    dp.include_router(h_common.router)
    dp.include_router(h_manager.router)
    dp.include_router(h_housekeeping.router)
    dp.include_router(h_reception.router)
    return dp


def build_bot() -> Bot:
    s = get_settings()
    return Bot(token=s.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
