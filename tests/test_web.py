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
    assert r.status_code == 200 and "Payme" in r.text


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
                "/panel/events", "/panel/rooms/qr-print"]:
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
