from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import clock
from app.main import app
from app.models import ZoneEvent
from app.services import bookings, maintenance, rules
from tests.helpers import accept_and_pay_cash, new_booking


def test_health_reports_worker(db, staff):
    c = TestClient(app)
    assert c.get("/api/ping").status_code == 200
    r = c.get("/api/health")
    assert r.status_code == 503 and r.json()["worker"]["ok"] is False
    rules.run_minutely(db)
    db.commit()
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json()["worker"]["ok"] is True
    clock.advance(minutes=6)
    assert c.get("/api/health").status_code == 503


def test_retention_purges_old_passports_and_snapshots(db, staff, tmp_path):
    from app.config import get_settings

    store = Path(get_settings().storage_dir)
    store.mkdir(parents=True, exist_ok=True)
    photo = store / "p1.jpg"
    photo.write_bytes(b"x")
    snap = store / "s1.jpg"
    snap.write_bytes(b"x")
    rec = staff["reception"]
    b = new_booking(db, rec, nights=1, photo_path=str(photo))
    accept_and_pay_cash(db, b, rec)
    clock.set_now(datetime(2026, 12, 16, 11, 0))
    bookings.checkout(db, b, rec)
    db.add(ZoneEvent(kind="door", event_type="dwell", occurred_at=clock.now(), snapshot_path=str(snap)))
    db.commit()
    assert maintenance.purge_old_files(db) == {"passports": 0, "snapshots": 0}
    clock.advance(days=91)
    assert maintenance.purge_old_files(db) == {"passports": 1, "snapshots": 1}
    db.commit()
    assert not photo.exists() and not snap.exists()
    db.refresh(b.guest)
    assert b.guest.document_photo is None and b.guest.full_name  # matn yozuvi qoladi
    _ = timedelta


def test_notifications_wait_for_outer_commit(db, staff, telegram):
    """Savepoint (rejalashtiruvchi qadamlari) ichida yaratilgan ogohlantirish tashqi commitdan keyin yuboriladi."""
    from app.services import alerts

    with db.begin_nested():
        alerts.raise_alert(db, "R9", "sinov", dedupe_key="t1")
    assert not telegram.sent  # hali commit bo'lmagan
    db.commit()
    assert any("sinov" in m["text"] for m in telegram.sent)
