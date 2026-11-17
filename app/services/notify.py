"""Xabar yuborish: Telegram (xodimlar/ega) va SMS (mehmon).

Telegram xabarlari tranzaksiya muvaffaqiyatli yakunlangandan keyin
(`after_commit`) yuboriladi — bazada yo'q narsa haqida xabar ketmasligi uchun.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.enums import Role, Severity
from app.i18n import t
from app.integrations import sms as sms_mod
from app.integrations import telegram as tg
from app.models import Alert, SmsLog, User

log = logging.getLogger(__name__)

SEVERITY_ICON = {Severity.MEDIUM: "🟡", Severity.HIGH: "🟠", Severity.CRITICAL: "🔴"}


# --------------------------------------------------------------- outbox
def _outbox(db: Session) -> list:
    return db.info.setdefault("tg_outbox", [])


def later(db: Session, chat_id: int | None, text: str, buttons: list | None = None) -> None:
    if chat_id:
        _outbox(db).append(("msg", chat_id, text, buttons))


def later_alert(db: Session, alert_id: int) -> None:
    _outbox(db).append(("alert", alert_id))


def later_call(db: Session, fn, *args) -> None:
    """Commitdan keyin ixtiyoriy funksiyani chaqirish (yangi sessiya bilan)."""
    _outbox(db).append(("call", fn, args))


@event.listens_for(Session, "after_commit")
def _flush_outbox(session: Session) -> None:
    items = session.info.pop("tg_outbox", None)
    if not items:
        return
    from app.db import SessionLocal

    for item in items:
        try:
            if item[0] == "msg":
                _, chat_id, text, buttons = item
                tg.backend().send_message(chat_id, text, buttons)
            elif item[0] == "alert":
                with SessionLocal() as s2:
                    alert = s2.get(Alert, item[1])
                    if alert is not None:
                        dispatch_alert(s2, alert)
                        s2.commit()
            elif item[0] == "call":
                _, fn, args = item
                with SessionLocal() as s2:
                    fn(s2, *args)
                    s2.commit()
        except Exception:  # pragma: no cover - tarmoq xatolari
            log.exception("Outbox elementi yuborilmadi: %s", item[:2])


@event.listens_for(Session, "after_rollback")
def _drop_outbox(session: Session) -> None:
    session.info.pop("tg_outbox", None)


# --------------------------------------------------------------- recipients
def users_with_roles(db: Session, roles: Iterable[str]) -> list[User]:
    return list(
        db.scalars(
            select(User).where(User.role.in_(list(roles)), User.active.is_(True), User.telegram_id.is_not(None))
        )
    )


def owners(db: Session) -> list[User]:
    return users_with_roles(db, [Role.OWNER])


def tell_roles(db: Session, roles: Iterable[str], key: str, **kw) -> None:
    for u in users_with_roles(db, roles):
        later(db, u.telegram_id, t(key, u.lang, **kw))


def tell_user(db: Session, user: User | None, key: str, buttons=None, **kw) -> None:
    if user is not None and user.telegram_id:
        later(db, user.telegram_id, t(key, user.lang, **kw), buttons)


# --------------------------------------------------------------- alerts
def alert_text(alert: Alert, lang: str = "uz") -> str:
    head = t(
        "al.header",
        lang,
        icon=SEVERITY_ICON.get(alert.severity, "⚪"),
        rule=alert.rule,
        severity=t(f"sev.{alert.severity}", lang),
    )
    lines = [head, alert.message]
    if alert.room is not None:
        lines.append(f"🏨 {alert.room.number}")
    if alert.booking_id:
        lines.append(f"📄 #{alert.booking_id}")
    if alert.user is not None:
        lines.append(f"👤 {alert.user.full_name}")
    lines.append(f"🕒 {clock.fmt(alert.at)}")
    return "\n".join(lines)


def alert_buttons(alert: Alert, lang: str = "uz") -> list[list[dict]]:
    rows = []
    if alert.video_link and alert.video_link.startswith("http"):
        rows.append([{"text": t("al.video", lang), "url": alert.video_link}])
    rows.append(
        [
            {"text": t("al.ack", lang), "callback_data": f"al:ack:{alert.id}"},
            {"text": t("al.resolve", lang), "callback_data": f"al:res:{alert.id}"},
            {"text": t("al.false", lang), "callback_data": f"al:fp:{alert.id}"},
        ]
    )
    return rows


def dispatch_alert(db: Session, alert: Alert) -> None:
    """Yuqori/kritik ogohlantirishni egaga darhol yuborish (FT-42)."""
    if alert.notified_at is not None:
        return
    recipients = owners(db)
    if alert.severity == Severity.CRITICAL:
        recipients += [u for u in users_with_roles(db, [Role.ADMIN]) if u not in recipients]
    sent = False
    for u in recipients:
        mid = tg.backend().send_message(u.telegram_id, alert_text(alert, u.lang), alert_buttons(alert, u.lang))
        sent = sent or mid is not None
    if sent or not recipients:
        alert.notified_at = clock.now()


# --------------------------------------------------------------- SMS
def send_sms(db: Session, phone: str, text: str, kind: str, booking_id: int | None = None) -> bool:
    res = sms_mod.provider().send(phone, text)
    db.add(
        SmsLog(
            phone=sms_mod.normalize_phone(phone),
            text=text,
            kind=kind,
            booking_id=booking_id,
            ok=res.ok,
            provider_ref=res.ref,
            error=res.error,
            at=clock.now(),
        )
    )
    return res.ok


def guest_url(token: str) -> str:
    return f"{get_settings().base_url.rstrip('/')}/g/{token}"
