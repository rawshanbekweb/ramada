"""Fon jarayoni: Telegram bot (long polling) + rejalashtiruvchi.

    python -m app.worker
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app import clock
from app.config import get_settings
from app.db import session_scope
from app.enums import Role
from app.models import Booking, KeyBoardCheck
from app.services import notify, reports, rules
from app.services import settings_svc as cfg

log = logging.getLogger("worker")


def _job(fn):
    def run():
        try:
            with session_scope() as db:
                fn(db)
        except Exception:
            log.exception("Rejalashtirilgan ish xatosi: %s", fn.__name__)

    run.__name__ = fn.__name__
    return run


def send_daily(db):
    text = reports.daily_text(db)
    for o in notify.owners(db):
        notify.later(db, o.telegram_id, text)


def send_weekly(db):
    text = reports.weekly_text(db)
    for o in notify.owners(db):
        notify.later(db, o.telegram_id, text)


def send_monthly(db):
    text = reports.monthly_text(db)
    for o in notify.owners(db):
        notify.later(db, o.telegram_id, text)


def keyboard_check(db):
    """FT-46: kunda bir marta tasodifiy vaqtda navbatchi receptiondan kalit taxtasi surati."""
    if not cfg.get(db, "keyboard_check_enabled"):
        return
    from app.services import shifts

    sh = shifts.any_open_shift(db)
    if sh is None or sh.user.role != Role.RECEPTION or not sh.user.telegram_id:
        return
    occupied = [
        b.room.number for b in db.scalars(select(Booking).where(Booking.status == "active")).all()
    ]
    db.add(KeyBoardCheck(user_id=sh.user_id, requested_at=clock.now(), expected_out=sorted(occupied)))
    notify.tell_user(db, sh.user, "hk.keyboard_req")


def build_scheduler() -> AsyncIOScheduler:
    tz = clock.tz()
    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(_job(rules.run_minutely), "interval", minutes=1, id="minutely", max_instances=1, coalesce=True)
    sched.add_job(_job(rules.run_every_15), "interval", minutes=15, id="every15", max_instances=1, coalesce=True)
    with session_scope() as db:
        hour = cfg.get_int(db, "daily_report_hour")
    sched.add_job(_job(send_daily), CronTrigger(hour=hour, minute=0, timezone=tz), id="daily")
    sched.add_job(_job(send_weekly), CronTrigger(day_of_week="mon", hour=hour, minute=5, timezone=tz), id="weekly")
    sched.add_job(_job(send_monthly), CronTrigger(day=1, hour=hour, minute=10, timezone=tz), id="monthly")
    # Kalit taxtasi: har kuni 10:00–22:00 orasida tasodifiy daqiqada
    def schedule_keyboard():
        when = clock.local().replace(hour=10, minute=0, second=0) + timedelta(minutes=random.randint(0, 12 * 60))
        if when > clock.local():
            sched.add_job(_job(keyboard_check), "date", run_date=when, id="keyboard", replace_existing=True)

    sched.add_job(schedule_keyboard, CronTrigger(hour=9, minute=55, timezone=tz), id="keyboard_plan")
    schedule_keyboard()
    return sched


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sched = build_scheduler()
    sched.start()
    log.info("Rejalashtiruvchi ishga tushdi")
    if get_settings().telegram_bot_token:
        from app.bot.main import build_bot, build_dispatcher

        bot = build_bot()
        dp = build_dispatcher()
        log.info("Telegram bot ishga tushmoqda")
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    else:
        log.warning("SQ_TELEGRAM_BOT_TOKEN yo'q — faqat rejalashtiruvchi ishlaydi")
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
