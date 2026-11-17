"""Tizim salomatligi va shaxsiy ma'lumotlarni saqlash muddati.

* Worker (rejalashtiruvchi + bot) har daqiqada «yurak urishi» yozadi. To'xtab qolsa,
  ogohlantirishlar ham to'xtaydi — shuning uchun panel va /api/health buni ko'rsatadi.
* Pasport suratlari va kamera kadrlari belgilangan muddatdan keyin diskdan o'chiriladi
  («Shaxsga doir ma'lumotlar to'g'risida»gi Qonun: maqsadga erishilgach saqlamaslik).
  Bron, to'lov va hodisa yozuvlari (matn) saqlanib qoladi.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.enums import BookingStatus
from app.models import Booking, Guest, ZoneEvent
from app.services import audit
from app.services import settings_svc as cfg

log = logging.getLogger(__name__)
WORKER_KEY = "worker_heartbeat"
WORKER_STALE_MINUTES = 5


def worker_beat(db: Session) -> None:
    cfg.state_set(db, WORKER_KEY, {"at": clock.now().isoformat()})


def worker_status(db: Session) -> dict:
    from app.services.entry import _parse_dt

    v = cfg.state_get(db, WORKER_KEY) or {}
    at = _parse_dt(v.get("at"))
    ok = at is not None and clock.now() - at <= timedelta(minutes=WORKER_STALE_MINUTES)
    return {"last": at, "ok": ok}


def _remove(path: str | None) -> bool:
    if not path:
        return False
    p = Path(path).resolve()
    root = Path(get_settings().storage_dir).resolve()
    if root not in p.parents:
        return False
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError as e:  # pragma: no cover
        log.warning("Fayl o'chirilmadi %s: %s", p, e)
        return False


def purge_old_files(db: Session) -> dict:
    """Muddati o'tgan pasport suratlari va hodisa kadrlarini o'chirish."""
    now = clock.now()
    passport_days = cfg.get_int(db, "passport_photo_days")
    snapshot_days = cfg.get_int(db, "snapshot_days")
    removed = {"passports": 0, "snapshots": 0}
    if passport_days > 0:
        cutoff = now - timedelta(days=passport_days)
        rows = db.execute(
            select(Guest, Booking)
            .join(Booking, Booking.guest_id == Guest.id)
            .where(
                Guest.document_photo.is_not(None),
                Booking.status.in_([BookingStatus.CHECKED_OUT, BookingStatus.CANCELLED]),
                ((Booking.checked_out_at.is_not(None)) & (Booking.checked_out_at < cutoff))
                | ((Booking.cancelled_at.is_not(None)) & (Booking.cancelled_at < cutoff)),
            )
        ).all()
        for guest, _b in rows:
            _remove(guest.document_photo)
            guest.document_photo = None
            guest.document_photo_tg = None
            removed["passports"] += 1
    if snapshot_days > 0:
        cutoff = now - timedelta(days=snapshot_days)
        for ev in db.scalars(select(ZoneEvent).where(ZoneEvent.snapshot_path.is_not(None), ZoneEvent.occurred_at < cutoff)):
            _remove(ev.snapshot_path)
            ev.snapshot_path = None
            removed["snapshots"] += 1
    if removed["passports"] or removed["snapshots"]:
        audit.log(db, "system", "retention.purge", object_type="storage", object_id=now.date().isoformat(), new=removed)
    return removed
