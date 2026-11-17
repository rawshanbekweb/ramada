"""Kamera mantig'i: dalil kuchi, kamera sog'lig'i, kuzatuv uzilishlari, NVR soati, sozlash rejimi."""
from datetime import timedelta

from sqlalchemy import select

from app import clock
from app.models import Alert, Camera, CameraZone, CoverageGap
from app.services import entry, rules
from app.services import settings_svc as cfg
from tests.helpers import accept_and_pay_cash, door_event, heartbeat, new_booking, room


def alerts_of(db, rule):
    return db.scalars(select(Alert).where(Alert.rule == rule)).all()


def test_passerby_single_weak_event_is_low_severity(db, staff, telegram):
    """Yonidan o'tgan odam (bitta qisqa regionEntrance) — darhol signal yo'q, 3 daqiqadan keyin faqat o'rta daraja."""
    ev = door_event(db, "202", event_type="regionEntrance")
    assert ev.verdict == "R2?" and ev.strength == "weak"
    assert not alerts_of(db, "R2")
    clock.advance(minutes=4)
    entry.resolve_suspects(db)
    db.commit()
    a = alerts_of(db, "R2")
    assert len(a) == 1 and a[0].severity == "medium"
    assert not any("R2" in m["text"] for m in telegram.sent)  # o'rta daraja — faqat hisobotda


def test_repeated_weak_events_become_strong(db, staff, telegram):
    first = door_event(db, "202", event_type="regionEntrance")
    second = door_event(db, "202", minutes_from_now=1.5, event_type="regionEntrance")  # 30 soniyadan keyin
    assert second.strength == "strong"
    db.refresh(first)
    assert first.verdict == "corroborated"
    a = alerts_of(db, "R2")
    assert len(a) == 1 and a[0].severity == "high"
    clock.advance(minutes=5)
    entry.resolve_suspects(db)
    db.commit()
    assert len(alerts_of(db, "R2")) == 1


def test_weak_other_room_event_gives_high_not_critical(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec)
    ev = door_event(db, "204", event_type="regionEntrance")
    assert ev.verdict == "R3?"
    clock.advance(minutes=4)
    entry.resolve_suspects(db)
    db.commit()
    a = alerts_of(db, "R3")
    assert len(a) == 1 and a[0].severity == "high"
    # Oyna yopilganda R1 alohida berilmaydi (R3 bor)
    clock.advance(minutes=30)
    heartbeat(db)
    entry.sweep_windows(db)
    db.commit()
    assert not alerts_of(db, "R1")


def test_non_human_target_ignored(db, staff):
    ev = door_event(db, "202", target="vehicle")
    assert ev.verdict == "non_human"
    assert not alerts_of(db, "R2")


def test_calibration_mode_suppresses_and_lists_unmapped(db, staff):
    cfg.set_value(db, "calibration_until", (clock.now() + timedelta(hours=2)).isoformat())
    ev = door_event(db, "202")
    assert ev.verdict == "calibration" and not alerts_of(db, "R2")
    entry.ingest_event(db, channel=7, region_id=3, event_type="regionEntrance", occurred_at=clock.now())
    db.commit()
    assert entry.unmapped_regions(db) == [{"channel": 7, "region_id": 3, "count": 1, "last": clock.now()}]
    clock.advance(hours=3)  # rejim o'zi tugaydi
    assert door_event(db, "203").verdict == "R2"


def test_camera_tamper_alert_and_window_unverifiable(db, staff, telegram):
    rec = staff["reception"]
    heartbeat(db)
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec)
    entry.ingest_event(db, channel=1, region_id=0, event_type="shelteralarm", state="active", occurred_at=clock.now())
    db.commit()
    a = alerts_of(db, "R12")
    assert a and a[-1].severity == "high" and "yopildi" in a[-1].message
    assert any("R12" in m["text"] for m in telegram.sent if m["chat_id"] == 1001)
    assert db.scalar(select(Camera).where(Camera.channel == 1)).tampered
    clock.advance(minutes=31)
    heartbeat(db)
    entry.sweep_windows(db)
    db.commit()
    db.refresh(b)
    assert not alerts_of(db, "R1")
    assert b.stage == 2 and any(c.source == "camera_unverifiable" for c in b.confirmations)
    entry.ingest_event(db, channel=1, region_id=0, event_type="shelteralarm", state="inactive", occurred_at=clock.now())
    db.commit()
    gap = db.scalar(select(CoverageGap).where(CoverageGap.channel == 1))
    assert gap.end_at is not None and not db.scalar(select(Camera).where(Camera.channel == 1)).tampered


def test_channel_offline_only_affects_its_floor(db, staff):
    rec = staff["reception"]
    entry.heartbeat(db, {"nvr_ok": True, "channels": {"1": True, "2": True}})
    b101 = new_booking(db, rec, number="101", phone="+998901111111")
    accept_and_pay_cash(db, b101, rec)
    b201 = new_booking(db, rec, number="201", phone="+998902222222")
    accept_and_pay_cash(db, b201, rec)
    entry.heartbeat(db, {"nvr_ok": True, "channels": {"1": False, "2": True}})
    db.commit()
    assert any(a.severity == "high" and "oflayn" in a.message for a in alerts_of(db, "R12"))
    clock.advance(minutes=31)
    entry.heartbeat(db, {"nvr_ok": True, "channels": {"1": False, "2": True}})
    entry.sweep_windows(db)
    db.commit()
    db.refresh(b101)
    db.refresh(b201)
    assert any(c.source == "camera_unverifiable" for c in b101.confirmations)  # 1-qavat — tekshirib bo'lmadi
    r1 = alerts_of(db, "R1")
    assert len(r1) == 1 and r1[0].booking_id == b201.id  # 2-qavat kamerasi ishlagan — haqiqiy R1


def test_window_waits_until_edge_queue_drained(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec)
    clock.advance(minutes=31)
    entry.heartbeat(db, {"nvr_ok": True, "queue": 12})  # edge'da hali yuborilmagan hodisalar bor
    entry.sweep_windows(db)
    db.commit()
    assert not alerts_of(db, "R1")
    # Navbatdan oyna ichidagi hodisa keladi
    entry.ingest_event(db, channel=1, region_id=1, event_type="fielddetection",
                       occurred_at=b.stage1_at + timedelta(minutes=4), edge_event_id="late-1")
    entry.heartbeat(db, {"nvr_ok": True, "queue": 0})
    entry.sweep_windows(db)
    db.commit()
    db.refresh(b)
    assert b.stage == 2 and not alerts_of(db, "R1")


def test_edge_reported_stream_gap(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec, number="101")
    accept_and_pay_cash(db, b, rec)
    start = clock.now() + timedelta(minutes=5)
    clock.advance(minutes=31)
    entry.heartbeat(db, {"nvr_ok": True, "queue": 0,
                         "gaps": [{"start": start.isoformat(), "end": (start + timedelta(minutes=3)).isoformat()}]})
    entry.sweep_windows(db)
    db.commit()
    db.refresh(b)
    assert not alerts_of(db, "R1")
    assert any(c.source == "camera_unverifiable" for c in b.confirmations)


def test_nvr_clock_skew_corrected(db, staff):
    real = clock.now()
    ev = entry.ingest_event(db, channel=2, region_id=2, event_type="fielddetection",
                            occurred_at=real + timedelta(minutes=17), edge_received_at=real)
    db.commit()
    assert ev.occurred_at == real and ev.skew_s == 17 * 60
    assert any(a.data.get("kind") == "clock" for a in alerts_of(db, "R12"))


def test_silent_camera_and_unmapped_room(db, staff):
    rec = staff["reception"]
    entry.heartbeat(db, {"nvr_ok": True, "channels": {"1": True, "2": True, "3": True}})
    db.commit()
    clock.advance(hours=25)
    entry.heartbeat(db, {"nvr_ok": True})
    door_event(db, "202")  # 2-qavat kamerasi ishlayapti
    rules.run_every_15(db)
    db.commit()
    silent = [a for a in alerts_of(db, "R12") if a.data.get("kind") == "silent"]
    assert {a.data["channel"] for a in silent} == {1, 3}
    # Zonasi bog'lanmagan xona — R1 emas, «tekshirib bo'lmadi»
    for z in db.scalars(select(CameraZone).where(CameraZone.room_id == room(db, "305").id)):
        db.delete(z)
    db.commit()
    b = new_booking(db, rec, number="305")
    accept_and_pay_cash(db, b, rec)
    clock.advance(minutes=31)
    heartbeat(db)
    entry.sweep_windows(db)
    db.commit()
    db.refresh(b)
    assert any(c.evidence.get("reason", "").startswith("xona eshigi") for c in b.confirmations)


def test_camera_health_rows(db, staff):
    door_event(db, "202")
    rows = entry.camera_health_rows(db)
    assert len(rows) == 4
    r2 = [r for r in rows if r["cam"].channel == 2][0]
    assert r2["doors"] == 14 and r2["events_24h"] == 1 and r2["coverage_pct"] == 100.0
