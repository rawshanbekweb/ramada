import json
import time

from fastapi.testclient import TestClient

from app.main import app
from app.security import edge_signature, make_room_token
from app.services import bookings
from tests.helpers import accept_and_pay_cash, new_booking, room


def test_guest_page_accept_and_device_binding(db, staff):
    b = new_booking(db, staff["reception"])
    c = TestClient(app, base_url="https://hotel.test")
    r = c.get(f"/g/{b.guest_token}?lang=ru")
    assert r.status_code == 200 and "Подтверждаю" in r.text and "1 000 000" in r.text
    r = c.post(f"/g/{b.guest_token}/accept", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    b = db.get(type(b), b.id)
    assert b.guest_accepted_at is not None and b.guest_device_hash
    # Boshqa qurilma — oxirgi 4 raqam so'raladi
    other = TestClient(app, base_url="https://hotel.test")
    r = other.get(f"/g/{b.guest_token}")
    assert "oxirgi 4" in r.text
    r = other.post(f"/g/{b.guest_token}/unlock", data={"last4": "4567"})
    assert r.status_code == 200 and "receptionda" in r.text


def test_qr_page_flow(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec)
    c = TestClient(app, base_url="https://hotel.test")
    c.post(f"/g/{b.guest_token}/accept")
    db.expire_all()
    b = db.get(type(b), b.id)
    bookings.record_payment(db, b, "cash", b.final_amount, user=rec, receipt_no="X")
    db.commit()
    token = make_room_token(room(db, "101").id, 1)
    r = c.get(f"/q/{token}")
    assert "rimi?" in r.text, r.text[-1500:]
    import re

    action = re.search(r'action="(/q/[^"]+/answer/\d+)', r.text).group(1)
    r = c.post(action.split("?")[0], data={"answer": "yes"})
    assert r.status_code == 200 and "Xush kelibsiz" in r.text
    assert c.get(f"/q/{token}x").status_code == 404


def test_edge_events_signature(db, staff):
    c = TestClient(app)
    body = json.dumps({"events": [{"id": "e1", "channel": 2, "region_id": 2, "event_type": "regionEntrance",
                                   "occurred_at": "2026-12-15T15:01:00+05:00"}], "nvr_ok": True}).encode()
    ts = str(int(time.time()))
    r = c.post("/api/edge/events", content=body, headers={"x-edge-timestamp": ts, "x-edge-signature": "bad"})
    assert r.status_code == 401
    sig = edge_signature("edge-test-secret", ts, body)
    r = c.post("/api/edge/events", content=body, headers={"x-edge-timestamp": ts, "x-edge-signature": sig})
    assert r.status_code == 200 and r.json()["accepted"] == ["e1"]
    r = c.post("/api/edge/events", content=body, headers={"x-edge-timestamp": ts, "x-edge-signature": sig})
    assert r.json()["duplicates"] == 1


def test_panel_pages(db, staff):
    rec = staff["reception"]
    b = new_booking(db, rec)
    accept_and_pay_cash(db, b, rec)
    c = TestClient(app, base_url="https://hotel.test")
    assert c.get("/panel/", follow_redirects=False).status_code == 303
    assert c.post("/panel/login", data={"login": "owner", "password": "wrong"}).status_code == 401
    c.post("/panel/login", data={"login": "owner", "password": "owner-pass-1"})
    for url in ["/panel/", "/panel/bookings", f"/panel/bookings/{b.id}", "/panel/alerts", "/panel/rooms",
                "/panel/tariffs", "/panel/settings", "/panel/staff", "/panel/shifts", "/panel/housekeeping",
                "/panel/audit", "/panel/reports", "/panel/reports?kind=weekly", "/panel/reports?kind=monthly",
                "/panel/events", "/panel/rooms/qr-print", "/panel/cameras"]:
        r = c.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    r = c.get("/panel/export.xlsx?start=2026-12-01&end=2026-12-31")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    # CSRF himoyasi
    assert c.post("/panel/settings", data={"hotel_name": "X"}).status_code == 400
    # Administrator tariflarni ko'rmaydi
    a = TestClient(app, base_url="https://hotel.test")
    a.post("/panel/login", data={"login": "admin", "password": "admin-pass-1"})
    assert a.get("/panel/tariffs").status_code == 403


def test_camera_calibration_bind_flow(db, staff):
    import re

    from sqlalchemy import select

    from app import clock
    from app.models import CameraZone
    from app.services import entry

    c = TestClient(app, base_url="https://hotel.test")
    c.post("/panel/login", data={"login": "owner", "password": "owner-pass-1"})
    page = c.get("/panel/cameras").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/panel/cameras/calibration", data={"csrf": csrf, "hours": "2"})
    assert "Zona sozlash rejimi yoqilgan" in c.get("/panel/cameras").text
    ev = entry.ingest_event(db, channel=9, region_id=5, event_type="fielddetection", occurred_at=clock.now())
    db.commit()
    assert "<td>9" in c.get("/panel/cameras").text
    rid = room(db, "305").id
    c.post("/panel/cameras/bind", data={"csrf": csrf, "channel": "9", "region_id": "5", "kind": "door", "room_id": str(rid)})
    db.expire_all()
    assert db.scalar(select(CameraZone).where(CameraZone.region_id == 5, CameraZone.room_id == rid))
    r = c.get(f"/panel/events/{ev.id}")
    assert r.status_code == 200 and "Dalil kuchi" in r.text
    assert "Ramada" in r.text


def test_panel_login_lockout(db, staff, telegram):
    c = TestClient(app, base_url="https://hotel.test")
    for _ in range(5):
        assert c.post("/panel/login", data={"login": "owner", "password": "x"}).status_code == 401
    assert any("bloklandi" in m["text"] for m in telegram.sent if m["chat_id"] == 1001)
    # To'g'ri parol bilan ham — vaqtincha blok
    assert c.post("/panel/login", data={"login": "owner", "password": "owner-pass-1"}).status_code == 429
    from app import clock

    clock.advance(minutes=16)
    r = c.post("/panel/login", data={"login": "owner", "password": "owner-pass-1"}, follow_redirects=False)
    assert r.status_code == 303


def test_panel_shift_and_payment(db, staff):
    import re

    from app.models import Booking

    b = new_booking(db, staff["reception"])
    bookings.guest_accept(db, b, "dev-x")
    db.commit()
    c = TestClient(app, base_url="https://hotel.test")
    c.post("/panel/login", data={"login": "admin", "password": "admin-pass-1"})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/panel/shifts").text).group(1)
    r = c.post(f"/panel/bookings/{b.id}/pay", data={"csrf": csrf, "method": "cash", "amount": "1000000", "receipt_no": "P-1"})
    assert r.status_code == 400  # smena yopiq
    c.post("/panel/shifts/open", data={"csrf": csrf, "amount": "0"})
    r = c.post(f"/panel/bookings/{b.id}/pay", data={"csrf": csrf, "method": "cash", "amount": "1 000 000", "receipt_no": "P-1"})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Booking, b.id).stage == 1
    c.post("/panel/shifts/close", data={"csrf": csrf, "amount": "1000000"})
    assert "Smenani ochish" in c.get("/panel/shifts").text
